"""Process mining as ordinary tables: an event log becomes `<log>_cases` (one row per case) and
`<log>_transitions` (one row per step-to-step move) in the workspace, so Ask, step-by-step answers,
investigations and their agents, metrics, monitors, schedules and dashboards all work on process data
through the paths they already use, with no process-specific code.

How: the log is read through the one gateway as the caller (scope, masking, row filters and audit apply),
per segment (e.g. per task type, each with its own expected path from the domain pack, else the most
frequent path), the rows are computed by `skills/process_mining` (the same definitions as the Process tab),
written as Parquet into the workspace's upload area under `process_mining/`, and registered as a file source
"Process mining tables" through the normal discover → select → crawl-with-profiling path. Re-running
replaces the tables (new content, new data version; findings built on the old version are voided as usual).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from analystos.connectors.naming import sanitize_identifier
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn, User
from analystos.governance.policy import require_role, resolve_scope
from analystos.skills import process_mining as pm
from analystos.skills.sqlbuild import col, count_star, table, to_sql

DERIVED_SOURCE_NAME = "Process mining tables"
DERIVED_FOLDER = "process_mining"
MAX_SEGMENTS = 12


def in_scope(session: Session, user: User, workspace_id: str) -> tuple[Any, list[SourceAsset]]:
    """The caller's resolved scope and the selected assets in it."""
    scope = resolve_scope(session, session.merge(user), workspace_id)
    fqs = set(scope.assets)
    assets = [a for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                                   SourceAsset.selected.is_(True))
                                         .order_by(SourceAsset.name, SourceAsset.id))
              if f"{a.schema_name}.{a.name}" in fqs]
    return scope, assets


def allowed_columns(scope: Any, fq: str) -> set[str]:
    denied = set(scope.denied_columns)
    return {c for c in scope.columns.get(fq, []) if f"{fq}.{c}" not in denied}


def segment_values(runner: Any, fq: str, column: str, limit: int = 50) -> list[dict[str, Any]]:
    q = to_sql(exp.select(col(column), count_star().as_("n")).from_(table(fq)).group_by(col(column))
               .order_by(exp.Ordered(this=count_star(), desc=True)), runner.dialect)
    res = runner(q, purpose="process_mining.segments", max_rows=limit)
    return [{"value": r[0], "count": r[1]} for r in res.rows if r[0] is not None]


def _derived_source(session: Session, user: User, workspace_id: str) -> Source:
    from analystos.services.sources import register_source

    src = session.scalar(select(Source).where(Source.workspace_id == workspace_id, Source.name == DERIVED_SOURCE_NAME,
                                              Source.kind == "csv"))
    if src is None:
        src = register_source(session, session.merge(user), workspace_id, kind="csv", name=DERIVED_SOURCE_NAME,
                              config={"path": f"{workspace_id}/{DERIVED_FOLDER}"}, secret_ref=None)
        session.flush()
    return src


def materialize(user: User, workspace_id: str, *, asset_id: str, case_column: str, activity_column: str,
                timestamp_column: str, resource_column: str | None = None, segment_column: str | None = None,
                max_events: int | None = None) -> dict[str, Any]:
    """Build (or rebuild) the case and transition tables of one event log and make them selected, profiled
    tables of the workspace. Needs the editor role (it adds a source and writes staged tables)."""
    import polars as pl

    from analystos.artifacts.registry import link
    from analystos.connectors.csv_file import default_upload_dir
    from analystos.core.config import get_settings
    from analystos.governance.audit import audit
    from analystos.runtime.context import default_gateway
    from analystos.services.crawler import crawl_source
    from analystos.services.sources import discover_source, select_assets

    with session_scope() as s:
        require_role(s, s.merge(user), workspace_id, "editor")
        scope, assets = in_scope(s, user, workspace_id)
        asset = next((a for a in assets if a.id == asset_id), None)
        if asset is None:
            exists = s.get(SourceAsset, asset_id)
            if exists is None or exists.workspace_id != workspace_id:
                raise NotFound("asset not found in this workspace")
            raise Forbidden("the table is not selected or not in your data scope")
        fq = f"{asset.schema_name}.{asset.name}"
        log_name, log_label = asset.name, asset.business_name or asset.name
        decl = pm.declared_for(pm.declared_event_logs(), [asset.source_name, asset.name])
    allowed = allowed_columns(scope, fq)
    mapping = pm.Mapping(case=case_column, activity=activity_column, timestamp=timestamp_column, resource=resource_column or None)
    seg_col = segment_column or (decl or {}).get("segment_column")
    named = mapping.columns() + ([seg_col] if seg_col else [])
    unknown = sorted({c for c in named if c not in allowed})
    if unknown:
        raise InvalidInput(f"{fq} has no readable column {', '.join(unknown)}", details={"columns": unknown})
    if len(set(mapping.columns())) != len(mapping.columns()):
        raise InvalidInput("case, activity, time and resource must be different columns")

    runner = default_gateway().run_sql_for(scope, actor=f"user:{user.id}", source_id=scope.asset_sources[fq])
    page = min(pm.DEFAULT_PAGE_ROWS, scope.max_rows or pm.DEFAULT_PAGE_ROWS)
    budget = max_events or pm.DEFAULT_MAX_EVENTS
    segments: list[Any] = [v["value"] for v in segment_values(runner, fq, seg_col)][:MAX_SEGMENTS] if seg_col else [None]
    case_out: list[dict[str, Any]] = []
    move_out: list[dict[str, Any]] = []
    summary, queries, truncated = [], [], False
    for value in segments:
        filters = [{"column": seg_col, "op": "=", "value": value}] if seg_col else []
        read = pm.read_event_log(runner, fq, mapping, filters=filters, page_rows=page, max_events=budget)
        queries += read["queries"]
        truncated = truncated or bool((read.get("coverage") or {}).get("truncated"))
        cases = pm.cases_from_rows(read["rows"])
        if not cases:
            continue
        declared_path = (((decl or {}).get("segments") or {}).get(str(value)) or {}).get("reference_path") if value is not None else None
        cancel = pm.cancel_activities({e.activity for evs in cases.values() for e in evs})
        if declared_path:
            reference, source = list(declared_path), f"{decl['pack']} expected path"
        else:
            reference = pm.variants(cases, top=1)["top"][0]["activities"]
            source = "inferred: the most frequent path"
        label = None if value is None else str(value)
        rows = pm.case_rows(cases, reference=reference, cancel=cancel, segment=label)
        case_out += rows
        move_out += pm.transition_rows(cases, segment=label)
        summary.append({"segment": label, "cases": len(rows), "expected_path": reference, "expected_path_source": source})
    if not case_out:
        raise InvalidInput(f"{fq} has no case with a case id, an activity and a time")

    base = sanitize_identifier(log_name, max_length=40, fallback="log")
    names = {"cases": f"{base}_cases", "transitions": f"{base}_transitions"}
    folder = Path(default_upload_dir(get_settings())) / workspace_id / DERIVED_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    frames = {"cases": pl.DataFrame(case_out, schema_overrides={"follows_expected_path": pl.Boolean,
                                                                "skipped_steps": pl.Utf8, "steps_out_of_order": pl.Utf8,
                                                                "segment": pl.Utf8, "first_resource": pl.Utf8,
                                                                "last_resource": pl.Utf8}).select(pm.CASE_TABLE_COLUMNS),
              "transitions": pl.DataFrame(move_out, schema_overrides={"segment": pl.Utf8, "from_resource": pl.Utf8,
                                                                      "to_resource": pl.Utf8}).select(pm.TRANSITION_TABLE_COLUMNS)}
    for kind, df in frames.items():
        df.write_parquet(folder / f"{names[kind]}.parquet")

    with session_scope() as s:
        src_id = _derived_source(s, user, workspace_id).id
    discover_source(user, src_id, workspace_id=workspace_id)
    with session_scope() as s:  # described before the crawl, so its glossary scan asks nothing about them
        for kind, name in names.items():
            a = s.scalar(select(SourceAsset).where(SourceAsset.source_id == src_id, SourceAsset.name == name))
            if a is not None:
                _describe(s, a, kind, log_label)
    with session_scope() as s:  # selecting re-stages from the files; keep other event logs' tables selected
        keep = [a.name for a in s.scalars(select(SourceAsset).where(SourceAsset.source_id == src_id, SourceAsset.selected.is_(True)))]
    select_assets(user, src_id, sorted(set(keep) | set(names.values())), workspace_id=workspace_id)
    crawl_source(user, src_id, mode="incremental", include=list(names.values()), profile=True, trigger="process_tables")

    out_tables = []
    with session_scope() as s:
        for kind, name in names.items():
            a = s.scalar(select(SourceAsset).where(SourceAsset.source_id == src_id, SourceAsset.name == name))
            if a is None:
                continue
            _describe(s, a, kind, log_label)
            derived_fq = f"{a.schema_name}.{a.name}"
            link(s, workspace_id, ("table", derived_fq), "derived_from", ("table", fq))
            out_tables.append({"kind": kind, "name": name, "fq": derived_fq, "asset_id": a.id, "rows": a.row_count,
                               "business_name": a.business_name})
        audit(f"user:{user.id}", "process.tables_built", workspace_id=workspace_id, target=fq,
              details={"source_id": src_id, "tables": [t["fq"] for t in out_tables], "segments": len(summary),
                       "cases": len(case_out), "transitions": len(move_out), "truncated": truncated}, session=s)
    return {"source_id": src_id, "event_log": {"asset_id": asset_id, "fq": fq}, "tables": out_tables, "segments": summary,
            "cases": len(case_out), "transitions": len(move_out), "truncated": truncated, "queries": queries}


def _describe(session: Session, asset: SourceAsset, kind: str, log_label: str) -> None:
    """Plain descriptions of the derived tables and their columns, as the source's own documentation (a person's
    curation still wins; a crawl never overwrites source text)."""
    if asset.description_origin not in ("user",) and not asset.reviewed:
        asset.description, asset.description_origin = pm.TABLE_DESCRIPTIONS[kind].format(log=log_label), "source"
    if asset.business_name_origin != "user":
        asset.business_name = f"Process {kind} ({log_label})"
        asset.business_name_origin = "source"
    for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id)):
        text = pm.COLUMN_DESCRIPTIONS.get(c.name)
        if text and c.description_origin != "user":
            c.description, c.description_origin = text, "source"
        if c.name == "case_id" and kind == "cases":
            c.is_key = True
