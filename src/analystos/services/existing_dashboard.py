"""Workspace-scoped inspection and versioned import of an existing Superset dashboard."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.artifacts.registry import link, save_artifact
from analystos.core.config import get_settings
from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound
from analystos.db.models import Approval, Artifact, Insight, SourceAsset, User
from analystos.governance.approvals import consume, request_approval, verify_for_execution
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, require_role
from analystos.knowledge.superset_meta import Scope
from analystos.publishing.superset import SupersetPublisher
from analystos.services.platform_settings import get as platform_settings


def _safe_inspection(raw: dict[str, Any], workspace_id: str, assets: list[SourceAsset]) -> dict[str, Any]:
    """Keep structural BI metadata; never persist filter values or virtual dataset SQL."""
    scope = Scope(workspace_id)
    if not scope.dashboard({"slug": raw.get("slug"), "dashboard_title": raw.get("title")}):
        raise Forbidden("dashboard belongs to another workspace")
    datasets = raw.get("datasets") or []
    charts = raw.get("charts") or []
    for d in datasets:
        if not scope.dataset({"table_name": d.get("name"), "database": {"database_name": d.get("database")}}):
            raise Forbidden("dashboard contains a dataset owned by another workspace")
    for c in charts:
        if not scope.chart({"slice_name": c.get("name")}):
            raise Forbidden("dashboard contains a chart owned by another workspace")
    metric_names = {m.get("name") for d in datasets for m in d.get("metrics") or [] if m.get("name")}
    mapped = []
    for d in datasets:
        name = str(d.get("name") or "").lower()
        schema = str(d.get("schema") or "").lower()
        matches = [a for a in assets if a.name.lower() == name and (not schema or a.schema_name.lower() == schema)]
        mapped.append({"dataset_id": d.get("id"), "asset_id": matches[0].id if len(matches) == 1 else None,
                       "state": "mapped" if len(matches) == 1 else ("ambiguous" if matches else "unmapped")})
    return {
        "id": raw.get("id"), "revision": raw.get("revision"), "title": raw.get("title"), "slug": raw.get("slug"),
        "published": raw.get("published"), "url": raw.get("url"),
        "datasets": [{**{k: d.get(k) for k in ("id", "name", "schema", "database", "main_dttm_col", "columns")},
                      "metrics": [{"name": m.get("name"), "verbose_name": m.get("verbose_name")}
                                  for m in d.get("metrics") or []], "has_virtual_query": bool(d.get("sql"))}
                     for d in datasets],
        "charts": [{**{k: c.get(k) for k in ("id", "name", "viz_type", "datasource", "groupby",
                                               "x_axis", "time_grain", "aos_key")},
                    "metrics": [m if m in metric_names else "[ad-hoc metric]" for m in c.get("metrics") or []],
                    "adhoc_filters": [{k: f.get(k) for k in ("subject", "operator", "expressionType")}
                                      for f in c.get("adhoc_filters") or []]}
                   for c in charts],
        "metrics": [{"name": m.get("name"), "verbose_name": m.get("verbose_name"),
                     "dataset_id": m.get("dataset_id")} for m in raw.get("metrics") or []],
        "filters": raw.get("filters") or [],
        "layout": raw.get("layout") or [], "workspace_mapping": mapped,
    }


def inspect(session: Session, user: User, workspace_id: str, dashboard_id: int,
            *, publisher: SupersetPublisher | None = None) -> dict[str, Any]:
    require_role(session, user, workspace_id, "viewer")
    if dashboard_id <= 0:
        raise InvalidInput("dashboard_id must be positive")
    if publisher is None and not platform_settings().features.superset_publishing:
        raise InvalidInput("Superset is turned off by the administrator")
    own = publisher is None
    publisher = publisher or SupersetPublisher(get_settings())
    try:
        raw = publisher.inspect_dashboard(dashboard_id)
    finally:
        if own:
            publisher.close()
    if raw.get("id") is None:
        raise NotFound("dashboard not found")
    assets = list(session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                           SourceAsset.lifecycle == "active")))
    return _safe_inspection(raw, workspace_id, assets)


def import_dashboard(session: Session, user: User, workspace_id: str, dashboard_id: int,
                     *, publisher: SupersetPublisher | None = None) -> Artifact:
    require_role(session, user, workspace_id, "editor")
    snapshot = inspect(session, user, workspace_id, dashboard_id, publisher=publisher)
    name = f"Imported Superset dashboard {dashboard_id}"
    art = save_artifact(session, workspace_id=workspace_id, type_="dashboard", name=name,
                        content={"origin": "superset_import", "snapshot": snapshot},
                        creator_user=user.id, status="imported")
    art.platform = "superset"
    art.external_id = str(dashboard_id)
    art.external_url = snapshot["url"]
    for mapping in snapshot["workspace_mapping"]:
        if mapping["asset_id"]:
            link(session, workspace_id, ("artifact", art.id), "uses", ("source_asset", mapping["asset_id"]))
    audit(f"user:{user.id}", "dashboard.import", workspace_id=workspace_id, target=art.id,
          details={"superset_id": dashboard_id, "version": art.version}, session=session)
    return art


TITLE_ACTION = "dashboard.existing_rename"
EDIT_ACTION = "dashboard.existing_edit"


def _imported(art: Artifact) -> dict[str, Any]:
    if art.type != "dashboard" or art.platform != "superset" or (art.content or {}).get("origin") != "superset_import":
        raise InvalidInput("this is not an imported Superset dashboard")
    return art.content["snapshot"]


def propose_title_change(session: Session, user: User, art: Artifact, title: str) -> Approval:
    require_role(session, user, art.workspace_id, "editor")
    snapshot = _imported(art)
    title = title.strip()
    if not title or len(title) > 200 or title == snapshot["title"]:
        raise InvalidInput("new_title must be distinct and contain 1 to 200 characters")
    payload = {"artifact_id": art.id, "artifact_hash": art.content_hash, "dashboard_id": art.external_id,
               "previous_title": snapshot["title"], "new_title": title}
    ws = get_workspace(session, art.workspace_id)
    return request_approval(session, workspace_id=art.workspace_id, run_id=None, action=TITLE_ACTION, payload=payload,
                            plan_hash=None, policy_version=ws.policy_version, requested_by=user.id, risk_tier="medium",
                            destination="superset", affected_assets=[art.id],
                            evidence={"dashboard_url": art.external_url, "version": art.version})


def apply_title_change(session: Session, user: User, art: Artifact, approval_id: str,
                       *, publisher: SupersetPublisher | None = None) -> Artifact:
    require_role(session, user, art.workspace_id, "editor")
    snapshot = _imported(art)
    approval = session.get(Approval, approval_id, with_for_update=True)
    if approval is None or approval.workspace_id != art.workspace_id or approval.action != TITLE_ACTION:
        raise InvalidInput("approval does not cover this dashboard change")
    payload = approval.payload or {}
    if payload.get("artifact_id") != art.id or payload.get("dashboard_id") != art.external_id:
        raise InvalidInput("approval does not cover this dashboard")
    expected = {"artifact_id": art.id, "artifact_hash": art.content_hash, "dashboard_id": art.external_id,
                "previous_title": snapshot["title"], "new_title": payload.get("new_title")}
    verify_for_execution(session, approval_id, payload=expected, plan_hash=None)
    own = publisher is None
    publisher = publisher or SupersetPublisher(get_settings())
    try:
        before = inspect(session, user, art.workspace_id, int(art.external_id), publisher=publisher)
        if before["title"] != snapshot["title"]:
            raise Conflict("dashboard changed in Superset since import; import it again before applying this approval")
        consume(session, approval)
        publisher.rename_existing_dashboard(int(art.external_id), expected["new_title"])
        after = inspect(session, user, art.workspace_id, int(art.external_id), publisher=publisher)
    finally:
        if own:
            publisher.close()
    if after["title"] != expected["new_title"]:
        raise Conflict("Superset did not retain the approved dashboard title")
    updated = save_artifact(session, workspace_id=art.workspace_id, type_="dashboard", name=art.name,
                            content={"origin": "superset_import", "snapshot": after}, creator_user=user.id,
                            status="imported")
    audit(f"user:{user.id}", "dashboard.existing_renamed", workspace_id=art.workspace_id, target=art.id,
          details={"superset_id": art.external_id, "approval_id": approval_id, "version": updated.version},
          session=session)
    return updated


def _validated_edit(snapshot: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    """Accept only existing workspace-scoped objects and constrained edit fields."""
    op = proposed.get("operation")
    if op not in {"chart_metric", "chart_groupby", "native_filter", "layout_size"}:
        raise InvalidInput("operation must be chart_metric, chart_groupby, native_filter or layout_size")
    edit: dict[str, Any] = {"operation": op}
    if op in {"chart_metric", "chart_groupby", "layout_size"}:
        chart_id = proposed.get("chart_id")
        chart = next((c for c in snapshot["charts"] if c["id"] == chart_id), None)
        if chart is None:
            raise InvalidInput("chart_id does not belong to this dashboard")
        edit["chart_id"] = chart_id
    if op in {"chart_metric", "chart_groupby"}:
        try:
            dataset_id = int(str(chart["datasource"]).split("__", 1)[0])
        except (TypeError, ValueError) as exc:
            raise InvalidInput("chart datasource is unavailable") from exc
        dataset = next((d for d in snapshot["datasets"] if d["id"] == dataset_id), None)
        if dataset is None or next((m for m in snapshot["workspace_mapping"]
                                    if m["dataset_id"] == dataset_id and m["state"] == "mapped"), None) is None:
            raise InvalidInput("chart dataset must map uniquely to a workspace source asset")
        if op == "chart_metric":
            metric = proposed.get("metric")
            if metric not in {m["name"] for m in dataset["metrics"]}:
                raise InvalidInput("metric must already exist on this chart's dataset")
            edit["metric"] = metric
        else:
            groupby = proposed.get("groupby")
            allowed = {c["name"] for c in dataset["columns"]}
            if not isinstance(groupby, list) or not groupby or len(groupby) > 3 or any(
                not isinstance(c, str) or c not in allowed for c in groupby
            ) or len(set(groupby)) != len(groupby):
                raise InvalidInput("groupby must contain 1 to 3 distinct dataset columns")
            edit["groupby"] = groupby
    elif op == "native_filter":
        dataset_id, column = proposed.get("dataset_id"), proposed.get("column")
        dataset = next((d for d in snapshot["datasets"] if d["id"] == dataset_id), None)
        mapped = next((m for m in snapshot["workspace_mapping"] if m["dataset_id"] == dataset_id
                       and m["state"] == "mapped"), None)
        if dataset is None or mapped is None or column not in {c["name"] for c in dataset["columns"]}:
            raise InvalidInput("native filter must target a mapped dataset column")
        if any(f["id"] == f"AOS_FILTER-{dataset_id}-{column}" for f in snapshot["filters"]):
            raise InvalidInput("this native filter already exists")
        edit.update(dataset_id=dataset_id, column=column)
    else:
        width, height = proposed.get("width"), proposed.get("height")
        if type(width) is not int or not 1 <= width <= 12 or type(height) is not int or not 16 <= height <= 200:
            raise InvalidInput("layout width must be 1-12 and height 16-200")
        if not any(c["chart_id"] == edit["chart_id"] for c in snapshot["layout"]):
            raise InvalidInput("chart has no dashboard layout cell")
        edit.update(width=width, height=height)
    return edit


def propose_edit(session: Session, user: User, art: Artifact, proposed: dict[str, Any]) -> Approval:
    require_role(session, user, art.workspace_id, "editor")
    snapshot = _imported(art)
    rationale = str(proposed.get("rationale") or "").strip()
    if not 10 <= len(rationale) <= 1000:
        raise InvalidInput("rationale must contain 10 to 1000 characters")
    edit = _validated_edit(snapshot, proposed)
    insight = session.get(Insight, proposed.get("insight_id"))
    if insight is None or insight.workspace_id != art.workspace_id or insight.status != "verified" or not insight.verified \
            or insight.stale_since is not None:
        raise InvalidInput("insight_id must reference a current verified finding in this workspace")
    payload = {"artifact_id": art.id, "artifact_hash": art.content_hash, "dashboard_id": art.external_id,
               "revision": snapshot.get("revision"), "edit": edit, "rationale": rationale,
               "insight_id": insight.id, "insight_hash": _insight_hash(insight)}
    ws = get_workspace(session, art.workspace_id)
    return request_approval(session, workspace_id=art.workspace_id, run_id=insight.run_id, action=EDIT_ACTION, payload=payload,
                            plan_hash=None, policy_version=ws.policy_version, requested_by=user.id, risk_tier="medium",
                            destination="superset", affected_assets=[art.id],
                            evidence={"dashboard_url": art.external_url, "version": art.version, "rationale": rationale,
                                      "insight_id": insight.id})


def apply_edit(session: Session, user: User, art: Artifact, approval_id: str,
               *, publisher: SupersetPublisher | None = None) -> Artifact:
    require_role(session, user, art.workspace_id, "editor")
    snapshot = _imported(art)
    approval = session.get(Approval, approval_id, with_for_update=True)
    if approval is None or approval.workspace_id != art.workspace_id or approval.action != EDIT_ACTION:
        raise InvalidInput("approval does not cover this dashboard edit")
    payload = approval.payload or {}
    if payload.get("artifact_id") != art.id or payload.get("dashboard_id") != art.external_id:
        raise InvalidInput("approval does not cover this dashboard")
    insight = session.get(Insight, payload.get("insight_id"))
    if insight is None or insight.workspace_id != art.workspace_id or insight.status != "verified" or not insight.verified \
            or insight.stale_since is not None or _insight_hash(insight) != payload.get("insight_hash"):
        raise Conflict("the verified finding changed or became stale since this edit was proposed")
    expected = {"artifact_id": art.id, "artifact_hash": art.content_hash, "dashboard_id": art.external_id,
                "revision": snapshot.get("revision"), "edit": payload.get("edit"), "rationale": payload.get("rationale"),
                "insight_id": insight.id, "insight_hash": _insight_hash(insight)}
    verify_for_execution(session, approval_id, payload=expected, plan_hash=None)
    edit = _validated_edit(snapshot, payload["edit"])
    own = publisher is None
    publisher = publisher or SupersetPublisher(get_settings())
    try:
        before = inspect(session, user, art.workspace_id, int(art.external_id), publisher=publisher)
        if snapshot.get("revision") is None or before["revision"] != snapshot["revision"]:
            raise Conflict("dashboard changed in Superset since import; import it again before editing")
        consume(session, approval)
        publisher.edit_existing_dashboard(int(art.external_id), edit)
        after = inspect(session, user, art.workspace_id, int(art.external_id), publisher=publisher)
    finally:
        if own:
            publisher.close()
    if after["revision"] == before["revision"] or not _edit_visible(after, edit):
        raise Conflict("Superset did not retain the approved dashboard edit")
    updated = save_artifact(session, workspace_id=art.workspace_id, type_="dashboard", name=art.name,
                            content={"origin": "superset_import", "snapshot": after}, creator_user=user.id,
                            status="imported")
    audit(f"user:{user.id}", "dashboard.existing_edited", workspace_id=art.workspace_id, target=art.id,
          details={"superset_id": art.external_id, "approval_id": approval_id, "operation": edit["operation"],
                   "version": updated.version}, session=session)
    return updated


def _insight_hash(insight: Insight) -> str:
    body = {"status": insight.status, "verified": insight.verified, "finding": insight.finding,
            "evidence_bundle": insight.evidence_bundle, "data_version": insight.data_version}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _edit_visible(snapshot: dict[str, Any], edit: dict[str, Any]) -> bool:
    op = edit["operation"]
    if op in {"chart_metric", "chart_groupby"}:
        chart = next((c for c in snapshot["charts"] if c["id"] == edit["chart_id"]), None)
        if chart is None:
            return False
        return (edit["metric"] in chart["metrics"] if op == "chart_metric"
                else chart["groupby"] == edit["groupby"])
    if op == "native_filter":
        return any(f["id"] == f"AOS_FILTER-{edit['dataset_id']}-{edit['column']}" for f in snapshot["filters"])
    return any(c["chart_id"] == edit["chart_id"] and c["width"] == edit["width"]
               and c["height"] == edit["height"] for c in snapshot["layout"])
