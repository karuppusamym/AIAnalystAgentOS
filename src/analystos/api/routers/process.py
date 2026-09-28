"""Process and task mining (Work → Process): detect event logs in the catalog, analyse one, and turn it into
ordinary tables (`POST /process/tables`) that Ask, investigations, agents, metrics and dashboards use.

`GET /process/candidates` lists the selected tables that look like an event log (a case id, an activity, a
time; the crawler's `event` role and a pack's declared model help) with a suggested column mapping and the
segments to split by. `POST /process/analyze` reads the log through the one gateway, as the caller, and
returns the deterministic analysis of `skills/process_mining.py` with the query ids it read; `save: true`
keeps it as a versioned `process_analysis` artifact (it then appears in Outputs, with lineage to its queries).
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.artifacts.registry import link, save_artifact
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.core.ids import utcnow
from analystos.db.models import Artifact, SourceAsset, SourceColumn, User
from analystos.governance.policy import require_role
from analystos.services.process_tables import allowed_columns as _allowed_columns
from analystos.services.process_tables import in_scope as _in_scope
from analystos.services.process_tables import segment_values as _segment_values
from analystos.skills import process_mining as pm

router = APIRouter(prefix="/api", tags=["process"])
ARTIFACT_TYPE = "process_analysis"


class ProcessFilter(BaseModel):
    column: str
    op: Literal["=", "!=", "in", "not_in"] = "="
    value: str | int | float | bool | None = None
    values: list[str | int | float | bool] | None = None


class ProcessAnalyzeIn(BaseModel):
    asset_id: str
    case_column: str
    activity_column: str
    timestamp_column: str
    resource_column: str | None = None
    filters: list[ProcessFilter] = Field(default_factory=list)
    # The happy path conformance compares with; omitted = the pack's model for the filtered segment, else inferred
    reference_path: list[str] | None = None
    max_events: int | None = Field(default=None, ge=1, le=1_000_000)
    save: bool = False
    name: str | None = Field(default=None, max_length=200)


def _asset_view(a: SourceAsset, cols: list[SourceColumn], allowed: set[str]) -> dict[str, Any]:
    return {"name": a.name, "role": (a.semantics or {}).get("role"), "row_count": a.row_count,
            "columns": [{"name": c.name, "data_type": c.data_type, "is_key": c.is_key, "tags": c.tags or [],
                         "profile": c.profile or {}} for c in cols if c.name in allowed]}


@router.get("/workspaces/{workspace_id}/process/candidates")
def candidates(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Selected tables that look like an event log, best first, with the suggested mapping."""
    from analystos.runtime.context import default_gateway

    scope, assets = _in_scope(session, user, workspace_id)
    declared = pm.declared_event_logs()
    cols_by = {}
    for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_([a.id for a in assets]))
                             .order_by(SourceColumn.asset_id, SourceColumn.ordinal)):
        cols_by.setdefault(c.asset_id, []).append(c)
    gateway = None
    out = []
    for a in assets:
        fq = f"{a.schema_name}.{a.name}"
        allowed = _allowed_columns(scope, fq)
        found = pm.detect_event_log(_asset_view(a, cols_by.get(a.id, []), allowed))
        decl = pm.declared_for(declared, [a.source_name, a.name])
        if decl and all(v in allowed for v in decl["mapping"].values() if v):
            found = found or {"segments": [], "score": 0.0, "reasons": []}
            found = {**found, "mapping": {k: decl["mapping"].get(k) for k in
                                          ("case_column", "activity_column", "timestamp_column", "resource_column")},
                     "score": found["score"] + 5,
                     "reasons": [f"declared as an event log by {decl['pack']}"] + found["reasons"]}
        if not found:
            continue
        segments = found["segments"]
        seg_col = decl.get("segment_column") if decl else None
        if seg_col and seg_col in allowed:
            segments = [{"column": seg_col, "values": []}] + [s for s in segments if s["column"] != seg_col]
        if segments and not segments[0]["values"]:
            if gateway is None:
                gateway = default_gateway()
            runner = gateway.run_sql_for(scope, actor=f"user:{user.id}", source_id=scope.asset_sources[fq])
            segments[0] = {**segments[0], "values": _segment_values(runner, fq, segments[0]["column"])}
        if decl and seg_col:
            labels = decl.get("segments") or {}
            for s in segments:
                if s["column"] == seg_col:
                    s["values"] = [{**v, "label": (labels.get(str(v["value"])) or {}).get("label"),
                                    "reference_path": (labels.get(str(v["value"])) or {}).get("reference_path")}
                                   for v in s["values"]]
        out.append({"asset_id": a.id, "asset": fq, "name": a.name, "business_name": a.business_name,
                    "row_count": a.row_count, "role": (a.semantics or {}).get("role"), "declared_by": decl["pack"] if decl else None,
                    "mapping": found["mapping"], "segments": segments, "score": found["score"],
                    "reasons": found["reasons"], "columns": sorted(allowed)})
    out.sort(key=lambda c: (-c["score"], c["asset"]))
    return {"version": pm.VERSION, "candidates": out}


@router.post("/workspaces/{workspace_id}/process/analyze")
def analyze(workspace_id: str, body: ProcessAnalyzeIn, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    """Analyse one event log through the gateway (as the caller: scope, masking, row filters and audit apply)."""
    from analystos.runtime.context import default_gateway

    scope, assets = _in_scope(session, user, workspace_id)
    asset = next((a for a in assets if a.id == body.asset_id), None)
    if asset is None:
        exists = session.get(SourceAsset, body.asset_id)
        if exists is None or exists.workspace_id != workspace_id:
            raise NotFound("asset not found in this workspace")
        raise Forbidden("the table is not selected or not in your data scope")
    fq = f"{asset.schema_name}.{asset.name}"
    allowed = _allowed_columns(scope, fq)
    mapping = pm.Mapping(case=body.case_column, activity=body.activity_column, timestamp=body.timestamp_column,
                         resource=body.resource_column or None)
    named = mapping.columns() + [f.column for f in body.filters]
    unknown = sorted({c for c in named if c not in allowed})
    if unknown:
        raise InvalidInput(f"{fq} has no readable column {', '.join(unknown)}", details={"columns": unknown})
    if len(set(mapping.columns())) != len(mapping.columns()):
        raise InvalidInput("case, activity, time and resource must be different columns")
    filters = [f.model_dump(exclude_none=True) for f in body.filters]
    reference, ref_source = body.reference_path, "declared in the request" if body.reference_path else None
    decl = pm.declared_for(pm.declared_event_logs(), [asset.source_name, asset.name])
    segment = None
    if decl and decl.get("segment_column"):
        segment = next((f.value for f in body.filters if f.column == decl["segment_column"] and f.op == "=" and f.value is not None),
                       None)
    if reference is None and segment is not None:
        model = (decl.get("segments") or {}).get(str(segment)) or {}
        if model.get("reference_path"):
            reference, ref_source = list(model["reference_path"]), f"{decl['pack']} reference model for {segment}"
    runner = default_gateway().run_sql_for(scope, actor=f"user:{user.id}", source_id=scope.asset_sources[fq])
    page = min(pm.DEFAULT_PAGE_ROWS, scope.max_rows or pm.DEFAULT_PAGE_ROWS)
    read = pm.read_event_log(runner, fq, mapping, filters=filters, page_rows=page,
                             max_events=body.max_events or pm.DEFAULT_MAX_EVENTS)
    cases = pm.cases_from_rows(read["rows"])
    result = pm.analyze_cases(cases, reference_path=reference, reference_source=ref_source)
    label = " · ".join(str(x) for x in [asset.business_name or asset.name, segment] if x)
    result = {
        **result,
        "title": body.name or f"Process analysis · {label}",
        "asset": {"id": asset.id, "fq": fq, "name": asset.name, "business_name": asset.business_name},
        "mapping": {"case_column": mapping.case, "activity_column": mapping.activity,
                    "timestamp_column": mapping.timestamp, "resource_column": mapping.resource},
        "filters": filters, "segment": segment,
        "provenance": {"queries": read["queries"], "sql": read["sql"], "coverage": read["coverage"],
                       "computed_at": utcnow().isoformat(), "dialect": runner.dialect,
                       "method": "ordered events read through the query gateway in keyset pages by case; "
                                 "analytics computed deterministically in skills/process_mining.py"},
    }
    if body.save:  # resolve_scope already required the analyst role
        art = save_artifact(session, workspace_id=workspace_id, type_=ARTIFACT_TYPE, name=result["title"][:300],
                            content=result, creator_user=user.id)
        session.flush()
        for q in read["queries"]:
            link(session, workspace_id, (ARTIFACT_TYPE, art.id), "derived_from", ("query", q))
            link(session, workspace_id, ("query", q), "reads", ("table", fq))
        result["artifact"] = {"id": art.id, "name": art.name, "version": art.version}
    return result


@router.get("/workspaces/{workspace_id}/process/analyses")
def saved_analyses(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Saved process analyses, newest first (the full content is `GET /api/artifacts/{id}`)."""
    require_role(session, user, workspace_id, "viewer")
    arts = session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.type == ARTIFACT_TYPE)
                           .order_by(Artifact.created_at.desc()).limit(100))
    return [{"id": a.id, "name": a.name, "version": a.version, "created_at": a.created_at.isoformat() if a.created_at else None,
             "updated_at": a.updated_at.isoformat() if a.updated_at else None,
             "asset": (a.content or {}).get("asset"), "segment": (a.content or {}).get("segment"),
             "mapping": (a.content or {}).get("mapping"), "summary": (a.content or {}).get("summary")} for a in arts]


class ProcessTablesIn(BaseModel):
    asset_id: str
    case_column: str
    activity_column: str
    timestamp_column: str
    resource_column: str | None = None
    # One row per case of each segment value (e.g. per task type); omitted = the pack's declared segment column, if any
    segment_column: str | None = None
    max_events: int | None = Field(default=None, ge=1, le=1_000_000)


@router.post("/workspaces/{workspace_id}/process/tables")
def build_tables(workspace_id: str, body: ProcessTablesIn, user: User = Depends(current_user)):
    """Turn an event log into the workspace tables `<log>_cases` and `<log>_transitions` (source "Process mining
    tables"): read through the gateway as the caller, staged, profiled and described like any selected table, so
    every part of the platform can use them. Editor role; re-running replaces them."""
    from analystos.services.process_tables import materialize

    return materialize(user, workspace_id, **body.model_dump())
