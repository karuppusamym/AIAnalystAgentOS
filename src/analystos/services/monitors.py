"""Continuous analytics (§38, MON-001..005).

A monitor turns a validated KPI (or a table's data quality) into a time series through the same
governed gateway as every other query, evaluates it deterministically (threshold, robust drift,
change point, data-quality regression), raises de-duplicated alerts, asks JEV to triage
materiality (escalate-only), and — when the workspace policy allows — starts an investigation run.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.contracts.policy import ExecutionIdentity
from analystos.core.errors import AnalystOSError, InvalidInput, NotFound
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import Alert, Artifact, Monitor, User, Workspace
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import evaluate as policy_evaluate
from analystos.governance.policy import require_role, resolve_scope
from analystos.llm.router import CallContext
from analystos.services.notifications import notify

log = get_logger(__name__)
KINDS = {"metric_threshold", "metric_drift", "change_point", "forecast_deviation", "data_quality"}
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
PERCENT_TOLERANCE = 1e-9


def condition_key(workspace_id: str, kind: str, config: dict) -> str:
    """Identity of what a monitor watches. Two monitors with the same condition are the same signal:
    they share alerts instead of each raising its own (the repeated alert in the Phase-3 evidence)."""
    return stable_hash({"workspace": workspace_id, "kind": kind, "config": config})[:32]


# ------------------------------------------------------------------------------------ definitions
def create_monitor(session: Session, user: User, workspace_id: str, *, name: str, kind: str, config: dict,
                   auto_investigate: bool = False) -> Monitor:
    require_role(session, user, workspace_id, "editor")
    if kind not in KINDS:
        raise InvalidInput(f"kind must be one of {sorted(KINDS)}")
    if kind != "data_quality":
        if not (config.get("metric") or config.get("sql_expression")):
            raise InvalidInput("metric monitors need config.metric (a validated metric name) or config.sql_expression")
        if kind == "metric_threshold" and (config.get("op") not in OPS or not isinstance(config.get("value"), (int, float))):
            raise InvalidInput("metric_threshold needs config.op in >,>=,<,<= and a numeric config.value")
        if config.get("grain", "week") not in ("day", "week", "month"):
            raise InvalidInput("grain must be day, week or month")
    key = condition_key(workspace_id, kind, config)
    for existing in session.scalars(select(Monitor).where(Monitor.workspace_id == workspace_id, Monitor.kind == kind,
                                                          Monitor.enabled.is_(True))):
        if condition_key(workspace_id, kind, existing.config) == key and existing.auto_investigate == auto_investigate:
            # Idempotent: re-creating a monitor for the same condition returns the one already watching it.
            audit(f"user:{user.id}", "monitor.reused", workspace_id=workspace_id, target=existing.id,
                  details={"kind": kind, "requested_name": name}, session=session)
            return existing
    m = Monitor(id=new_id("mon"), workspace_id=workspace_id, name=name, kind=kind, config=config, enabled=True,
                auto_investigate=auto_investigate, created_by=user.id)
    session.add(m)
    audit(f"user:{user.id}", "monitor.created", workspace_id=workspace_id, target=m.id, details={"kind": kind}, session=session)
    return m


def _is_fraction(value: Any) -> bool:
    return isinstance(value, (int, float)) and -PERCENT_TOLERANCE <= value <= 1 + PERCENT_TOLERANCE


def _usable_metric(metric: Artifact | None) -> bool:
    """A percent KPI whose validated value is not a fraction is a legacy x100 definition (written before
    semantic validation required fractions); monitoring it would report the same rate at 100 times its scale."""
    if metric is None:
        return False
    content = metric.content or {}
    value = (content.get("validation") or {}).get("value")
    return not (content.get("format") == "percent" and value is not None and not _is_fraction(value))


def _dataset_and_metric(session: Session, monitor: Monitor) -> tuple[dict, str, str, str | None]:
    cfg = monitor.config
    stmt = select(Artifact).where(Artifact.workspace_id == monitor.workspace_id, Artifact.type == "dataset")
    if cfg.get("dataset_artifact_id"):
        stmt = stmt.where(Artifact.id == cfg["dataset_artifact_id"])
    dataset = session.scalar(stmt.order_by(Artifact.created_at.desc()))
    if dataset is None:
        raise InvalidInput("no analytical dataset in this workspace yet: run an analysis first")
    expression = cfg.get("sql_expression")
    label = cfg.get("metric") or monitor.name
    fmt = cfg.get("format")
    if not expression:
        metric = session.scalar(select(Artifact).where(Artifact.workspace_id == monitor.workspace_id, Artifact.type == "metric",
                                                       Artifact.name == cfg["metric"], Artifact.run_id == dataset.run_id))
        if not _usable_metric(metric):
            candidates = session.scalars(select(Artifact).where(Artifact.workspace_id == monitor.workspace_id,
                                                                Artifact.type == "metric", Artifact.name == cfg["metric"])
                                         .order_by(Artifact.created_at.desc()))
            metric = next((m for m in candidates if _usable_metric(m)), None)
        if metric is None:
            raise NotFound(f"metric {cfg['metric']} not found (or only on a legacy x100 percent scale)")
        expression = metric.content["sql_expression"]
        label = metric.content.get("display_name") or label
        fmt = metric.content.get("format")
    return dict(dataset.content), expression, label, fmt


def check_scale(label: str, fmt: str | None, points: list[tuple[str, float]]) -> None:
    """Percent KPIs are fractions end to end; a series outside [0, 1] is a scale defect, not a signal."""
    if fmt != "percent":
        return
    bad = [(p, v) for p, v in points if not _is_fraction(v)]
    if bad:
        raise InvalidInput(f"{label} is a percent KPI but its series has {len(bad)} value(s) outside [0, 1] "
                           f"(e.g. {bad[0][1]:.4g} for {bad[0][0]}): the definition is on the wrong scale")


def metric_series(session: Session, owner: User, monitor: Monitor) -> dict[str, Any]:
    """Governed time series of the monitored metric: [(period, value)] oldest first."""
    from analystos.runtime.context import default_gateway

    ds, expression, label, fmt = _dataset_and_metric(session, monitor)
    time_col = ds.get("raw_time_column")
    if not time_col:
        raise InvalidInput("the dataset has no time column to monitor over")
    grain = monitor.config.get("grain", "week")
    scope = resolve_scope(session, owner, monitor.workspace_id, minimum_role="analyst")
    # Future-dated rows are a data-quality defect, not activity: excluded and counted.
    sql = (f'SELECT date_trunc(\'{grain}\', d."{time_col}") AS period, {expression} AS value, MAX(d."{time_col}") AS last_ts '
           f'FROM ({ds["sql"]}) d WHERE d."{time_col}" IS NOT NULL AND d."{time_col}" <= now() GROUP BY 1 ORDER BY 1')
    gateway = default_gateway()
    result = gateway.execute(scope, sql, actor=f"monitor:{monitor.id}", purpose=f"monitor.{monitor.kind}", max_rows=2000)
    future = gateway.execute(scope, f'SELECT COUNT(*) FROM ({ds["sql"]}) d WHERE d."{time_col}" > now()',
                             actor=f"monitor:{monitor.id}", purpose="monitor.future_rows").rows[0][0]
    rows = [(p, v, ts) for p, v, ts in result.rows if v is not None]
    points = [(str(p)[:10], float(v)) for p, v, _ in rows]
    check_scale(label, fmt, points)
    dropped = None
    if monitor.config.get("exclude_last_period", True) and rows:
        # The last period is incomplete unless the data reaches (almost) its end.
        from datetime import timedelta

        period_start, _, last_ts = rows[-1]
        start = _as_datetime(period_start)
        end = {"day": start + timedelta(days=1), "week": start + timedelta(days=7)}.get(grain) or _next_month(start)
        if _as_datetime(last_ts) < end - timedelta(hours=12):
            dropped = points.pop()[0]
    return {"label": label, "expression": expression, "format": fmt, "grain": grain, "points": points, "query_id": result.query_id,
            "excluded_future_rows": int(future or 0), "dropped_incomplete_period": dropped}


def _as_datetime(value):
    from datetime import UTC, datetime

    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _next_month(start):
    return start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1, day=1)


def _robust_z(values: list[float], x: float) -> float | None:
    if len(values) < 4:
        return None
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values]) * 1.4826
    if mad == 0:
        sd = statistics.pstdev(values)
        return None if sd == 0 else (x - statistics.mean(values)) / sd
    return (x - med) / mad


def _evaluate_metric(monitor: Monitor, series: dict) -> dict[str, Any]:
    cfg = monitor.config
    pts = series["points"]
    if len(pts) < 2:
        return {"alert": False, "reason": "not enough periods", "points": len(pts)}
    period, latest = pts[-1]
    out: dict[str, Any] = {"period": period, "value": latest, "points": len(pts), "series_tail": pts[-12:]}
    if monitor.kind == "metric_threshold":
        breached = OPS[cfg["op"]](latest, float(cfg["value"]))
        out.update(alert=breached, severity=cfg.get("severity", "warning"), threshold=f"{cfg['op']} {cfg['value']}",
                   message=f"{series['label']} was {latest:.4g} for {period} ({'breaches' if breached else 'within'} threshold {cfg['op']} {cfg['value']}).")
    elif monitor.kind == "metric_drift":
        lookback = int(cfg.get("lookback", 8))
        baseline = [v for _, v in pts[-lookback - 1:-1]]
        z = _robust_z(baseline, latest)
        threshold = float(cfg.get("z_threshold", 3.0))
        base_med = statistics.median(baseline) if baseline else None
        out.update(z=None if z is None else round(z, 3), baseline_median=base_med, lookback=len(baseline))
        if z is None or not math.isfinite(z):
            out.update(alert=False, reason="baseline too short or constant")
        else:
            change = (latest - base_med) / base_med if base_med else None
            out.update(alert=abs(z) >= threshold, severity="critical" if abs(z) >= 2 * threshold else "warning",
                       direction="increase" if z > 0 else "decrease", pct_change=None if change is None else round(change, 4),
                       message=f"{series['label']} was {latest:.4g} for {period} vs a {len(baseline)}-period median of "
                               f"{base_med:.4g} (robust z = {z:.2f}{'' if change is None else f', {change:+.1%}'}).")
    elif monitor.kind == "change_point":
        from analystos.skills.stats import change_point

        stat = change_point([v for _, v in pts], [p for p, _ in pts], alpha=float(cfg.get("alpha", 0.01)))
        hl = stat.highlights or {}
        recent = int(cfg.get("recent_periods", 4))
        idx = hl.get("change_index")
        is_recent = idx is not None and idx >= len(pts) - recent
        out.update(alert=bool(stat.supported and is_recent), severity="warning", change_period=hl.get("change_period"),
                   before_mean=hl.get("before_mean"), after_mean=hl.get("after_mean"), shift_pct=hl.get("shift_pct"),
                   p_value=stat.p_value,
                   message=(f"{series['label']} shifted from {hl.get('before_mean', 0):.4g} to {hl.get('after_mean', 0):.4g} "
                            f"({(hl.get('shift_pct') or 0):+.1%}) starting {hl.get('change_period')}.") if hl.get("change_period")
                   else f"No recent regime change in {series['label']}.")
    elif monitor.kind == "forecast_deviation":
        from analystos.skills.forecast import forecast_deviation

        history = pts[-int(cfg.get("history", 60)):]
        stat = forecast_deviation([v for _, v in history], [p for p, _ in history], z=float(cfg.get("z", 2.5)),
                                  seasonal_periods=cfg.get("seasonal_periods"))
        hl = stat.highlights or {}
        dev = hl.get("deviation_pct")
        out.update(alert=bool(stat.supported), severity="critical" if abs(hl.get("z_score") or 0) >= 2 * float(cfg.get("z", 2.5))
                   else "warning", expected=hl.get("expected"), lower=hl.get("lower"), upper=hl.get("upper"),
                   z=hl.get("z_score"), direction=hl.get("direction"), pct_change=dev, forecast_method=hl.get("method"),
                   warnings=stat.warnings,
                   message=(f"{series['label']} was {latest:.4g} for {period}; the forecast from {len(history) - 1} prior periods "
                            f"expected {hl.get('expected', 0):.4g} (interval {hl.get('lower', 0):.4g} to {hl.get('upper', 0):.4g})"
                            + ("" if dev is None else f", {dev:+.1%}") + ".") if hl.get("expected") is not None
                   else f"Not enough history to forecast {series['label']}.")
    return out


def _evaluate_quality(session: Session, owner: User, monitor: Monitor) -> dict[str, Any]:
    from analystos.gateway.types import QueryResult  # noqa: F401  (typing aid)
    from analystos.runtime.context import default_gateway
    from analystos.skills.profiling import profile_asset
    from analystos.skills.quality import check_quality

    scope = resolve_scope(session, owner, monitor.workspace_id, minimum_role="analyst")
    assets = monitor.config.get("assets") or scope.assets
    current: dict[str, dict] = {}
    for asset in assets:
        if asset not in scope.assets:
            continue
        run_sql = default_gateway().run_sql_for(scope, actor=f"monitor:{monitor.id}", source_id=scope.asset_sources[asset])
        cols = [{"name": c, "data_type": "text"} for c in scope.columns[asset] if f"{asset}.{c}" not in scope.denied_columns]
        from analystos.db.models import SourceAsset, SourceColumn

        schema, name = asset.split(".", 1)
        a = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == monitor.workspace_id,
                                                     SourceAsset.schema_name == schema, SourceAsset.name == name))
        types = {c.name: c.data_type for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id))} if a else {}
        cols = [{"name": c["name"], "data_type": types.get(c["name"], "text")} for c in cols]
        profile = profile_asset(run_sql, asset, cols)
        for issue in check_quality(run_sql, asset, profile):
            key = f"{issue.asset}|{issue.column}|{issue.code}"
            current[key] = {"severity": issue.severity, "message": issue.message,
                            "rate": float((issue.metric or {}).get("rate") or (issue.metric or {}).get("share") or 0)}
    baseline = (monitor.last_result or {}).get("issues")
    if baseline is None:
        return {"alert": False, "baseline_set": True, "issues": current, "message": f"Baseline recorded: {len(current)} issues."}
    new = {k: v for k, v in current.items() if k not in baseline and v["severity"] in ("warning", "critical")}
    worse = {k: v for k, v in current.items() if k in baseline and v["rate"] > 1.5 * max(baseline[k].get("rate") or 0, 1e-9)
             and v["severity"] in ("warning", "critical")}
    alert = bool(new or worse)
    severity = "critical" if any(v["severity"] == "critical" for v in {**new, **worse}.values()) else "warning"
    msg = "; ".join([f"new: {v['message']}" for v in list(new.values())[:3]] + [f"worse: {v['message']}" for v in list(worse.values())[:3]])
    return {"alert": alert, "severity": severity, "issues": current, "new": list(new), "worse": list(worse),
            "message": msg or "No data-quality regression."}


# ------------------------------------------------------------------------------------ evaluation
def evaluate_monitor(monitor_id: str, *, trigger: str = "manual") -> dict[str, Any]:
    with session_scope() as s:
        monitor = s.get(Monitor, monitor_id)
        if monitor is None:
            raise NotFound("monitor not found")
        owner = s.get(User, monitor.created_by)
        try:
            if monitor.kind == "data_quality":
                result = _evaluate_quality(s, owner, monitor)
            else:
                series = metric_series(s, owner, monitor)
                result = {**_evaluate_metric(monitor, series), "metric": series["label"], "query_id": series["query_id"],
                          "grain": series["grain"], "excluded_future_rows": series["excluded_future_rows"],
                          "dropped_incomplete_period": series["dropped_incomplete_period"]}
        except AnalystOSError as exc:
            monitor.state, monitor.last_evaluated_at = "error", utcnow()
            monitor.last_result = {**(monitor.last_result or {}), "error": exc.message}
            emit(monitor.workspace_id, "monitor.evaluated", {"monitor": monitor.id, "state": "error", "error": exc.message}, session=s)
            raise
        workspace = s.get(Workspace, monitor.workspace_id)
        s.expunge_all()
    alert_id = None
    if result.get("alert"):
        alert_id = _raise_alert(monitor, owner, workspace, result)
    with session_scope() as s:
        m = s.get(Monitor, monitor_id)
        m.state = "alerting" if result.get("alert") else "ok"
        m.last_evaluated_at = utcnow()
        m.last_result = {k: v for k, v in result.items() if k != "series_tail"} | {"series_tail": result.get("series_tail")}
        if not result.get("alert"):
            key = condition_key(m.workspace_id, m.kind, m.config)
            for a in s.scalars(select(Alert).where(or_(Alert.monitor_id == monitor_id, Alert.dedupe_key.like(f"{key}:%")),
                                                   Alert.status != "resolved")):
                a.status, a.resolved_at = "resolved", utcnow()
        emit(m.workspace_id, "monitor.evaluated", {"monitor": m.id, "state": m.state, "trigger": trigger,
                                                   "message": result.get("message"), "alert_id": alert_id}, session=s)
    return {**result, "alert_id": alert_id}


def _triage(monitor: Monitor, workspace: Workspace, result: dict) -> tuple[str, dict | None]:
    """JEV materiality check. Escalate-only: it can raise severity, never lower it."""
    from analystos.llm.jev import JevDecisions
    from analystos.runtime.context import default_router

    severity = result.get("severity", "warning")
    verdict = JevDecisions(default_router()).probability(
        "alert_triage", {"objective": workspace.objective, "monitor": monitor.name, "signal": result.get("message", "")},
        "Is `signal` a material change for `objective` that an operations lead would want investigated now?",
        ctx=CallContext(workspace_id=workspace.id, agent_id="monitor"))
    if verdict is None:
        return severity, None
    from analystos.services.platform_settings import get as platform

    if verdict.value >= platform().monitors.triage_escalate_probability and SEVERITY_RANK[severity] < SEVERITY_RANK["critical"]:
        severity = "critical"
    return severity, {"p_material": verdict.value, "model": verdict.model}


def alert_dedupe_key(monitor: Monitor, result: dict) -> str:
    """Same condition, same period: one open alert, whichever monitor observed it."""
    at = result.get("period") or result.get("change_period") or ",".join(result.get("new", []))
    return f"{condition_key(monitor.workspace_id, monitor.kind, monitor.config)}:{at}"[:200]


def _open_alert(*keys: str) -> str | None:
    with session_scope() as s:
        return s.scalar(select(Alert.id).where(Alert.dedupe_key.in_(keys), Alert.status != "resolved"))


def _raise_alert(monitor: Monitor, owner: User, workspace: Workspace, result: dict) -> str:
    dedupe = alert_dedupe_key(monitor, result)
    legacy = f"{monitor.id}:{dedupe.split(':', 1)[1]}"[:200]  # alerts raised before condition keys
    existing_id = _open_alert(dedupe, legacy)
    if existing_id:
        return existing_id  # no second alert, notification, triage call or investigation for the same signal
    severity, triage = _triage(monitor, workspace, result)
    with session_scope() as s:
        existing = s.scalar(select(Alert).where(Alert.dedupe_key == dedupe, Alert.status != "resolved"))
        if existing:
            return existing.id
        alert = Alert(id=new_id("alr"), workspace_id=monitor.workspace_id, monitor_id=monitor.id, severity=severity,
                      title=f"{monitor.name}: {result.get('direction', 'signal')} detected"[:300],
                      message=result.get("message", ""), data={**{k: v for k, v in result.items() if k != "issues"},
                                                               "triage": triage}, dedupe_key=dedupe)
        s.add(alert)
        s.flush()
        emit(monitor.workspace_id, "alert.raised", {"alert": alert.id, "severity": severity, "title": alert.title}, session=s)
        notify(s, monitor.workspace_id, kind="alert", title=f"[{severity}] {alert.title}", body=alert.message,
               link={"type": "alert", "id": alert.id})
        audit(f"monitor:{monitor.id}", "alert.raised", workspace_id=monitor.workspace_id, target=alert.id,
              details={"severity": severity, "triage": triage}, session=s)
        alert_id = alert.id
    from analystos.services.platform_settings import get as platform

    if platform().monitors.auto_investigation_enabled and monitor.auto_investigate and \
            SEVERITY_RANK[severity] >= SEVERITY_RANK["warning"] and (triage is None or triage["p_material"] >= 0.5):
        start_investigation(alert_id, owner, automatic=True)
    return alert_id


def start_investigation(alert_id: str, user: User, *, automatic: bool = False) -> str | None:
    """MON-005: open an analysis run on an alert. Automatic starts are allowed only when the
    workspace autonomy is >= 3 and policy allows run_analysis for the monitor owner."""
    from analystos.services.runs import create_run

    with session_scope() as s:
        alert = s.get(Alert, alert_id)
        if alert is None:
            raise NotFound("alert not found")
        if alert.investigation_run_id:
            return alert.investigation_run_id
        ws = s.get(Workspace, alert.workspace_id)
        if automatic:
            decision = policy_evaluate(s, s.merge(user), ExecutionIdentity(user_id=user.id, workspace_id=ws.id, agent_id="monitor",
                                                                          purpose="auto_investigation"), "run_analysis")
            if decision.decision != "allow" or ws.autonomy_level < 3:
                emit(ws.id, "policy.denied", {"action": "auto_investigation", "alert": alert_id, "reasons": decision.reasons}, session=s)
                return None
        objective = (f"Investigate this monitored change and identify its drivers: {alert.message} "
                     f"Business objective: {ws.objective}")[:2000]
        ws_id = ws.id
    run = create_run(user, ws_id, objective=objective, origin={"type": "alert", "alert_id": alert_id, "publish": "skip",
                                                               "automatic": automatic})
    with session_scope() as s:
        a = s.get(Alert, alert_id)
        a.investigation_run_id = run.id
        notify(s, ws_id, kind="run", title=f"Investigation started for alert: {a.title}"[:300],
               body="Started automatically by policy." if automatic else "", link={"type": "run", "id": run.id})
    return run.id
