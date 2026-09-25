"""Scheduling (§37, SCH-001..005).

Firing is claim-then-execute: due schedules are claimed with FOR UPDATE SKIP LOCKED, their next
fire time advanced, and a schedule_run inserted under a unique fire_key in the same transaction —
so several scheduler processes, restarts and manual "run now" never double-fire. Execution uses
the schedule owner's permissions as they are at fire time, not when the schedule was created.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Monitor, Schedule, ScheduleRun, Source, SourceAsset, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.services.notifications import notify

log = get_logger(__name__)
KINDS = {"reanalysis": "analyst", "dataset_refresh": "editor", "report": "analyst", "monitor": "analyst", "crawl": "editor"}
MIN_INTERVAL_SECONDS = 15 * 60


def next_fire(cron: str, tz: str, after: datetime | None = None) -> datetime:
    zone = ZoneInfo(tz)
    base = (after or utcnow()).astimezone(zone)
    return croniter(cron, base).get_next(datetime).astimezone(ZoneInfo("UTC"))


def validate(kind: str, cron: str, tz: str, config: dict) -> None:
    if kind not in KINDS:
        raise InvalidInput(f"kind must be one of {sorted(KINDS)}")
    if not croniter.is_valid(cron):
        raise InvalidInput(f"invalid cron expression: {cron}")
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise InvalidInput(f"unknown timezone {tz}") from exc
    first = next_fire(cron, tz)
    if (next_fire(cron, tz, first) - first).total_seconds() < MIN_INTERVAL_SECONDS:
        raise InvalidInput("schedules may not fire more often than every 15 minutes")
    report = config.get("report")
    if report is not None and not isinstance(report, dict):
        raise InvalidInput("config.report must be an object like {kind, formats}")
    if config.get("publish", "skip") not in ("skip", "propose"):
        raise InvalidInput("config.publish must be skip or propose (publication always needs an approval)")


def create_schedule(session: Session, user: User, workspace_id: str, *, name: str, kind: str, cron: str, timezone: str = "UTC",
                    config: dict | None = None) -> Schedule:
    require_role(session, user, workspace_id, "editor")
    config = config or {}
    validate(kind, cron, timezone, config)
    sch = Schedule(id=new_id("sch"), workspace_id=workspace_id, name=name, kind=kind, cron=cron, timezone=timezone, config=config,
                   owner_id=user.id, enabled=True, next_run_at=next_fire(cron, timezone))
    session.add(sch)
    audit(f"user:{user.id}", "schedule.created", workspace_id=workspace_id, target=sch.id,
          details={"kind": kind, "cron": cron, "timezone": timezone}, session=session)
    return sch


def update_schedule(session: Session, user: User, schedule_id: str, patch: dict) -> Schedule:
    sch = session.get(Schedule, schedule_id)
    if sch is None:
        raise NotFound("schedule not found")
    require_role(session, user, sch.workspace_id, "editor")
    for key in ("name", "cron", "timezone", "config", "enabled"):
        if patch.get(key) is not None:
            setattr(sch, key, patch[key])
    validate(sch.kind, sch.cron, sch.timezone, sch.config)
    sch.next_run_at = next_fire(sch.cron, sch.timezone) if sch.enabled else None
    audit(f"user:{user.id}", "schedule.updated", workspace_id=sch.workspace_id, target=sch.id, details=patch, session=session)
    return sch


# ------------------------------------------------------------------------------------ firing
def _claim(session: Session, sch: Schedule, scheduled_for: datetime, trigger: str) -> str | None:
    fire_key = f"{sch.id}:{trigger}:{scheduled_for.isoformat()}"
    run_id = new_id("srun")
    inserted = session.execute(insert(ScheduleRun).values(
        id=run_id, schedule_id=sch.id, workspace_id=sch.workspace_id, fire_key=fire_key, scheduled_for=scheduled_for,
        trigger=trigger, status="started", result={}).on_conflict_do_nothing(index_elements=["fire_key"]).returning(ScheduleRun.id)).scalar()
    return inserted


def claim_due(now: datetime | None = None, limit: int = 10) -> list[str]:
    now = now or utcnow()
    claimed = []
    with session_scope() as s:
        due = list(s.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
                             .order_by(Schedule.next_run_at).limit(limit).with_for_update(skip_locked=True)))
        for sch in due:
            scheduled_for = sch.next_run_at
            sch.next_run_at = next_fire(sch.cron, sch.timezone, max(now, scheduled_for))
            sch.last_run_at = now
            srun = _claim(s, sch, scheduled_for, "cron")
            if srun:
                claimed.append(srun)
    return claimed


def run_now(user: User, schedule_id: str) -> str:
    with session_scope() as s:
        sch = s.get(Schedule, schedule_id)
        if sch is None:
            raise NotFound("schedule not found")
        require_role(s, user, sch.workspace_id, KINDS[sch.kind])
        srun = _claim(s, sch, utcnow(), "manual")
    execute(srun)
    return srun


def _finish(srun_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
    with session_scope() as s:
        srun = s.get(ScheduleRun, srun_id)
        if srun is None or srun.status in ("succeeded", "failed", "skipped"):
            return
        srun.status, srun.result, srun.error = status, {**(srun.result or {}), **(result or {})}, error
        srun.finished_at = utcnow()
        sch = s.get(Schedule, srun.schedule_id)
        emit(srun.workspace_id, "schedule.executed", {"schedule": srun.schedule_id, "schedule_run": srun.id, "status": status,
                                                      "kind": sch.kind if sch else None, "error": error}, session=s)
        audit(f"schedule:{srun.schedule_id}", "schedule.executed", workspace_id=srun.workspace_id, target=srun.id,
              decision="allow" if status == "succeeded" else None, details={"status": status, "error": error}, session=s)
        if status == "failed" and sch:
            notify(s, srun.workspace_id, kind="schedule", title=f"Schedule failed: {sch.name}", body=error or "",
                   link={"type": "schedule", "id": sch.id}, user_id=sch.owner_id)


def execute(srun_id: str) -> None:
    """Run one claimed firing. Never raises: failures are recorded on the schedule_run."""
    with session_scope() as s:
        srun = s.get(ScheduleRun, srun_id)
        sch = s.get(Schedule, srun.schedule_id)
        owner = s.get(User, sch.owner_id)
        kind, config, workspace_id, schedule_id = sch.kind, dict(sch.config), sch.workspace_id, sch.id
        try:
            if owner is None or not owner.active:
                raise NotFound("schedule owner is inactive")
            require_role(s, owner, workspace_id, KINDS[kind])  # permissions as of now
        except AnalystOSError as exc:
            s.expunge_all()
            _finish(srun_id, "failed", error=f"authorization: {exc.message}")
            return
        srun.status = "running"
        s.flush()
        s.expunge_all()
    try:
        result = {"dataset_refresh": _refresh, "reanalysis": _reanalysis, "report": _report, "monitor": _monitors,
                  "crawl": _crawl}[kind](
            owner, workspace_id, schedule_id, srun_id, config)
    except AnalystOSError as exc:
        _finish(srun_id, "failed", error=f"{exc.code}: {exc.message}")
        return
    except Exception as exc:  # recorded, never propagated into the scheduler loop
        log.exception("schedule %s failed", schedule_id)
        _finish(srun_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return
    if result.pop("_pending", False):  # re-analysis: the run's finalize step completes the schedule_run
        with session_scope() as s:
            s.get(ScheduleRun, srun_id).result = result
        return
    _finish(srun_id, "succeeded", result)


def _refresh(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    from analystos.services.sources import select_assets

    with session_scope() as s:
        sources = list(s.scalars(select(Source).where(Source.workspace_id == workspace_id, Source.execution_mode == "staged",
                                                      Source.status == "ready")))
        if config.get("source_ids"):
            sources = [x for x in sources if x.id in config["source_ids"]]
        plan = {x.id: [a.name for a in s.scalars(select(SourceAsset).where(SourceAsset.source_id == x.id, SourceAsset.selected.is_(True)))]
                for x in sources}
    loaded = {}
    for source_id, assets in plan.items():
        if assets:
            loaded[source_id] = select_assets(owner, source_id, assets)["loaded"]
    return {"refreshed": loaded}


def _crawl(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    """Crawl every registered source of the workspace (or config.source_ids); incremental by default."""
    from analystos.services.crawler import crawl_source

    with session_scope() as s:
        stmt = select(Source.id).where(Source.workspace_id == workspace_id, Source.status.in_(("discovered", "ready")))
        if config.get("source_ids"):
            stmt = stmt.where(Source.id.in_(config["source_ids"]))
        ids = list(s.scalars(stmt))
    out: dict[str, Any] = {}
    for sid in ids:
        try:
            res = crawl_source(owner, sid, mode=config.get("mode"), include=config.get("include"), exclude=config.get("exclude"),
                               trigger="schedule", actor=f"schedule:{schedule_id}")
            out[sid] = {"crawl_id": res["crawl_id"], **{k: res["stats"].get(k) for k in ("new", "changed", "deprecated", "profiled")}}
        except AnalystOSError as exc:
            out[sid] = {"error": exc.message}
    if ids and all("error" in v for v in out.values()):
        raise InvalidInput("every crawl failed: " + "; ".join(v["error"] for v in out.values())[:500])
    return {"crawls": out}


def _previous_run(workspace_id: str, schedule_id: str) -> str | None:
    with session_scope() as s:
        for srun in s.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == schedule_id, ScheduleRun.status == "succeeded")
                              .order_by(ScheduleRun.started_at.desc()).limit(10)):
            rid = (srun.result or {}).get("run_id")
            if rid and (r := s.get(AnalysisRun, rid)) and r.status == "COMPLETED":
                return rid
        if (cfg_prev := s.get(Schedule, schedule_id).config.get("baseline_run_id")):
            return cfg_prev
    return None


def _reanalysis(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    from analystos.services.runs import create_run

    refreshed = _refresh(owner, workspace_id, schedule_id, srun_id, config) if config.get("refresh_first", True) else {}
    previous = _previous_run(workspace_id, schedule_id)
    run = create_run(owner, workspace_id, objective=config.get("objective"), source_ids=config.get("source_ids"),
                     origin={"type": "schedule", "schedule_id": schedule_id, "schedule_run_id": srun_id,
                             "previous_run_id": previous, "publish": config.get("publish", "skip"),
                             "report": config.get("report", {"kind": "weekly_summary", "formats": ["html", "pdf", "xlsx"]})})
    return {"_pending": True, "run_id": run.id, "previous_run_id": previous, "refreshed": refreshed}


def _report(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    from analystos.services.reports import generate_report

    with session_scope() as s:
        run_id = config.get("run_id") or s.scalar(select(AnalysisRun.id).where(
            AnalysisRun.workspace_id == workspace_id, AnalysisRun.status == "COMPLETED").order_by(AnalysisRun.finished_at.desc()))
        if not run_id:
            raise InvalidInput("no completed run to report on")
        art = generate_report(s, run_id, kind=config.get("kind", "executive"), formats=tuple(config.get("formats", ["html", "pdf"])),
                              actor=f"schedule:{schedule_id}")
        return {"run_id": run_id, "report_artifact_id": art.id}


def _monitors(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict) -> dict[str, Any]:
    from analystos.services.monitors import evaluate_monitor

    with session_scope() as s:
        stmt = select(Monitor.id).where(Monitor.workspace_id == workspace_id, Monitor.enabled.is_(True))
        if config.get("monitor_ids"):
            stmt = stmt.where(Monitor.id.in_(config["monitor_ids"]))
        ids = list(s.scalars(stmt))
    results = {}
    for mid in ids:
        try:
            r = evaluate_monitor(mid, trigger=f"schedule:{schedule_id}")
            results[mid] = {"alert": bool(r.get("alert")), "alert_id": r.get("alert_id"), "message": r.get("message")}
        except AnalystOSError as exc:
            results[mid] = {"error": exc.message}
    return {"monitors": results, "alerts": sum(1 for r in results.values() if r.get("alert"))}


def complete_from_run(run_id: str) -> None:
    """Called when a scheduled run reaches a terminal state."""
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        origin = run.origin or {}
        srun_id = origin.get("schedule_run_id")
        status, summary = run.status, dict(run.summary or {})
    if not srun_id:
        return
    if status == "COMPLETED":
        changes = summary.get("changes") or {}
        _finish(srun_id, "succeeded", {"run_id": run_id, "report_artifact_id": summary.get("report_artifact_id"),
                                       "changes": {k: len(changes.get(k, [])) for k in ("new", "persisting", "changed", "resolved")}})
    else:
        _finish(srun_id, "failed", {"run_id": run_id}, error=f"run {status}: {summary.get('error') or ''}".strip())


def nightly_calibration() -> None:
    """Decision calibration (P4-T09) once a day, whichever scheduler process gets the advisory lock."""
    from analystos.decisions.calibration import maybe_run_nightly

    try:
        with session_scope() as s:
            maybe_run_nightly(s)
    except Exception:
        log.exception("decision calibration failed")


def run_scheduler(poll_seconds: float = 15.0, *, once: bool = False) -> None:
    from analystos.core.logging import configure_logging

    configure_logging()
    log.info("scheduler started (poll %ss)", poll_seconds)
    while True:
        try:
            for srun in claim_due():
                execute(srun)
        except Exception:
            log.exception("scheduler iteration failed")
        nightly_calibration()
        if once:
            return
        time.sleep(poll_seconds)
