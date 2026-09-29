"""What an Ask answer becomes outside its thread (P4-U02 promotions).

* **Dashboard**: after the hash-bound approval is verified (services/ask.py), the answer is published as
  a table chart through the same BI publisher a run uses (`publishing.base.get_publisher`), onto the
  named dashboard next to the answers published there before. The destination is the approved one:
  Superset when this installation has it, else the in-platform `preview` destination.
* **Report**: an HTML document of the answer (reports/answer.py) built only from what the turn stored:
  its result rows, question, SQL, provenance and evidence state. No model call and no new query.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, InvalidInput, PolicyDenied, UpstreamUnavailable
from analystos.core.ids import new_id, stable_hash
from analystos.db.models import Approval, Artifact, AskTurn, Publication, User

DEFAULT_DASHBOARD = "Ask answers"


def usable_destination(allowed: list[str], requested: str | None) -> tuple[str, str | None]:
    """The destination a request is bound to, and a note when it differs from what was asked: a named one must
    be allowed by the policy; Superset without the `bi` profile, or turned off by the administrator, becomes
    the in-platform preview (as for run publications), so the approval is never bound to an unreachable tool."""
    from analystos.core.config import get_settings
    from analystos.publishing.base import default_destination
    from analystos.services.platform_settings import get as platform

    if requested and requested not in allowed:
        raise PolicyDenied(f"destination {requested} is not allowed by this workspace's policy")
    if requested == "powerbi":
        raise InvalidInput("Power BI publishing is not implemented yet; use 'superset' or 'preview'")
    destination = requested or default_destination(allowed)
    if destination == "superset" and not get_settings().superset_url:
        return "preview", "Superset is not part of this installation; the chart is published to the in-platform preview."
    if destination == "superset" and not platform().features.superset_publishing:
        return "preview", "Superset publishing is turned off by the administrator; the chart is published to the in-platform preview."
    return destination, None


def _entry(turn_id: str, title: str, sql: str, columns: list[str], assets: list[str]) -> dict[str, Any]:
    return {"turn_id": turn_id, "title": title[:200], "sql": sql, "columns": [str(c) for c in columns], "assets": assets}


def _earlier_entries(session: Session, workspace_id: str, dashboard: str, destination: str, turn_id: str) -> list[dict[str, Any]]:
    """Answers already published to the same dashboard and destination: a dashboard is republished whole, so
    adding one chart keeps the others on it."""
    arts = session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.type == "chart",
                                                  Artifact.run_id.is_(None), Artifact.status == "published",
                                                  Artifact.platform == destination).order_by(Artifact.created_at))
    out = []
    for a in arts:
        c = a.content or {}
        origin = c.get("origin") or {}
        if origin.get("type") != "ask" or c.get("dashboard") != dashboard or origin.get("turn_id") == turn_id:
            continue
        if c.get("sql") and c.get("columns"):
            out.append(_entry(origin["turn_id"], c.get("title") or a.name, c["sql"], c["columns"], list(c.get("assets") or [])))
    return out


def answer_bundle(scope: Any, workspace_id: str, dashboard: str, destination: str, entries: list[dict[str, Any]]):
    """One virtual dataset and one table chart per answer; each SQL is re-validated against the caller's
    current scope first (an earlier answer the caller can no longer read is left off, and named)."""
    from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, PublishBundle
    from analystos.gateway.validator import validate_sql

    datasets, charts, dropped = [], [], []
    for i, e in enumerate(entries):
        try:
            validate_sql(scope, e["sql"], max_rows=1)
        except AnalystOSError as exc:
            if i == len(entries) - 1:  # the answer being published
                raise InvalidInput(f"this answer's SQL no longer passes the gateway for you: {exc.message}") from None
            dropped.append(e["title"])
            continue
        name = f"ask_{e['turn_id']}"
        datasets.append(DatasetDef(name=name, description=e["title"], sql=e["sql"], columns=[{"name": c} for c in e["columns"]],
                                   source_assets=e["assets"]))
        charts.append(ChartSpec(key=name, title=e["title"], chart_type="table", intent="detail", dataset=name,
                                description="An Ask answer, as its stored query returns it."))
    key = _dashboard_key(dashboard)
    dash = DashboardSpec(key=key, title=dashboard, audience="operational", charts=[c.key for c in charts],
                         description="Answers promoted from Ask, each under its own approval.")
    return PublishBundle(workspace_id=workspace_id, destination=destination, datasets=datasets, metrics=[], charts=charts,
                         dashboards=[dash]), dropped


def _dashboard_key(title: str) -> str:
    import re

    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", title.lower())).strip("_")[:60] or "ask_answers"


def publish_to_dashboard(session: Session, user: User, turn: AskTurn, payload: dict[str, Any], approval: Approval) -> dict[str, Any]:
    """Publish a verified, approved answer chart. The approval is claimed (single use) immediately before the
    external call; a failed publication raises, which rolls the claim back so a retry can reconcile."""
    from analystos.artifacts.registry import link, save_artifact
    from analystos.core.config import get_settings
    from analystos.events.bus import emit
    from analystos.governance.approvals import consume
    from analystos.governance.audit import audit
    from analystos.governance.policy import resolve_scope
    from analystos.publishing import base as publishing
    from analystos.publishing.preview import validate_bundle
    from analystos.services.platform_settings import get as platform

    destination, dashboard = payload["destination"], payload["dashboard"]
    if destination == "superset" and not platform().features.superset_publishing:
        raise PolicyDenied("Superset publishing was turned off by the administrator after this approval was granted")
    result = turn.result or {}
    columns = list(result.get("columns") or [])
    if not turn.sql or not columns:
        raise InvalidInput("this answer has no stored query result to publish")
    assets = [a["asset"] for a in (turn.provenance or {}).get("assets", []) if a.get("asset")]
    scope = resolve_scope(session, user, turn.workspace_id)
    entries = [*_earlier_entries(session, turn.workspace_id, dashboard, destination, turn.id),
               _entry(turn.id, turn.question, turn.sql, columns, assets)]
    bundle, dropped = answer_bundle(scope, turn.workspace_id, dashboard, destination, entries)
    problems = validate_bundle(bundle)
    if problems:
        raise InvalidInput("the answer cannot be published as a chart", details={"errors": problems})
    key = f"ask:{turn.id}:{approval.payload_hash[:32]}"
    consume(session, approval)
    try:
        published = publishing.get_publisher(destination, get_settings()).publish(bundle, idempotency_key=key)
    except AnalystOSError as exc:
        raise UpstreamUnavailable(f"publishing to {destination} failed: {exc.message}. The approval stays usable: retry.") from None
    if published.status != "succeeded":
        raise UpstreamUnavailable(f"publishing to {destination} {published.status}: {'; '.join(published.errors[:3])}. "
                                  "The approval stays usable: retry to reconcile.", details={"errors": published.errors})
    chart_key = f"ask_{turn.id}"
    dash_key = bundle.dashboards[0].key
    url = published.urls.get(dash_key)
    pub = Publication(id=new_id("pub"), workspace_id=turn.workspace_id, run_id=None, approval_id=approval.id,
                      destination=destination, idempotency_key=key, status="succeeded", external_ids=published.external_ids)
    session.add(pub)
    art = save_artifact(session, workspace_id=turn.workspace_id, type_="chart", name=chart_key, creator_user=user.id,
                        status="published",
                        content={"title": turn.question[:200], "chart_type": "table", "sql": turn.sql, "columns": columns,
                                 "assets": assets, "preview": {"columns": columns, "rows": list(result.get("rows") or [])[:200]},
                                 "chart": payload.get("chart"), "dashboard": dashboard, "destination": destination,
                                 "approval_id": approval.id, "origin": {"type": "ask", "turn_id": turn.id}})
    art.status, art.platform = "published", destination
    art.external_id = str((published.external_ids.get("charts") or {}).get(chart_key) or "") or None
    art.external_url = url
    session.flush()
    link(session, turn.workspace_id, ("chart", art.id), "published_as", ("publication", pub.id))
    emit(turn.workspace_id, "dashboard.published", {"destination": destination, "status": published.status, "urls": published.urls,
                                                    "turn_id": turn.id}, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "publication.executed", workspace_id=turn.workspace_id, target=pub.id, decision="allow",
          details={"destination": destination, "approval_id": approval.id, "turn_id": turn.id, "dashboard": dashboard,
                   "dropped": dropped}, session=session)
    return {"target": "dashboard", "id": art.id, "status": "published", "approval_id": approval.id, "dashboard": dashboard,
            "destination": destination, "url": url, "publication_id": pub.id, **({"dropped": dropped} if dropped else {})}


# ------------------------------------------------------------------------------ report
def answer_report_data(session: Session, turn: AskTurn) -> dict[str, Any]:
    """Everything the report shows, read from the stored turn (and its workspace's name)."""
    from analystos.db.models import Workspace
    from analystos.evidence.why import explain_ask_turn
    from analystos.services.ask import staleness, turn_out

    view = turn_out(session, turn)
    ws = session.get(Workspace, turn.workspace_id)
    prov = turn.provenance or {}
    try:
        with session.begin_nested():
            verdict = explain_ask_turn(session, turn)["verification_state"]
    except Exception:  # noqa: BLE001 - the report then says "not verified"; it never invents a verdict
        verdict = None
    semantic = prov.get("semantic") or {}
    return {
        "title": turn.question, "workspace_name": ws.name if ws else turn.workspace_id, "turn_id": turn.id,
        "thread_id": turn.thread_id, "answered_at": turn.created_at.isoformat() if turn.created_at else None,
        "answered_by": turn.answered_by, "model": turn.model, "governance": prov.get("governance", "ad_hoc"),
        "semantic": {"model_version": semantic.get("model_version"),
                     "metrics": [f"{m.get('name')} v{m.get('version')}" for m in semantic.get("metrics") or []]} if semantic else None,
        "sql": turn.sql or "", "result": {k: (turn.result or {}).get(k) for k in
                                          ("columns", "rows", "row_count", "truncated", "query_id", "result_hash")},
        "assets": [{k: a.get(k) for k in ("asset", "source_name", "source_kind", "execution_mode", "freshness_at", "row_count")}
                   for a in prov.get("assets") or []],
        "staleness": staleness(session, turn), "evidence_status": view.get("evidence_status"),
        "verification": {"badge": verdict.get("badge"), "state": verdict.get("state")} if verdict else None,
    }


def answer_report(session: Session, user: User, turn: AskTurn) -> dict[str, Any]:
    """Save the answer as a `report` artifact (HTML), listed in Outputs and downloadable like any report."""
    from analystos.artifacts.registry import save_artifact
    from analystos.core.config import get_settings
    from analystos.events.bus import emit
    from analystos.governance.audit import audit
    from analystos.reports.answer import render_answer_html

    data = answer_report_data(session, turn)
    data_hash = stable_hash(data)
    content = render_answer_html(data).encode("utf-8")
    directory = Path(get_settings().artifact_dir) / turn.workspace_id / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ask-{turn.id}-{data_hash[:12]}.html"
    path.write_bytes(content)
    files = {"html": {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                      "mime": "text/html; charset=utf-8", "ext": "html"}}
    art = save_artifact(session, workspace_id=turn.workspace_id, type_="report", name=f"answer_{turn.id}", creator_user=user.id,
                        status="final",
                        content={"kind": "ask_answer", "title": turn.question[:200], "report_data_hash": data_hash, "files": files,
                                 "rows": data["result"].get("row_count"),
                                 "origin": {"type": "ask", "turn_id": turn.id, "thread_id": turn.thread_id}})
    session.flush()
    emit(turn.workspace_id, "report.generated", {"artifact_id": art.id, "kind": "ask_answer", "formats": ["html"],
                                                 "turn_id": turn.id}, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "report.generated", workspace_id=turn.workspace_id, target=art.id,
          details={"kind": "ask_answer", "formats": ["html"], "turn_id": turn.id, "report_data_hash": data_hash}, session=session)
    return {"target": "report", "id": art.id, "status": "created", "name": turn.question[:200], "formats": ["html"]}


__all__ = ["DEFAULT_DASHBOARD", "answer_bundle", "answer_report", "answer_report_data", "publish_to_dashboard", "usable_destination"]
