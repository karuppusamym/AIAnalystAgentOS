"""Phase 3 API: schedules, monitors, alerts, notifications, reports (§37-§38, §43)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import Alert, AnalysisRun, Artifact, Monitor, Schedule, ScheduleRun, User
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.services import monitors as mon_svc
from analystos.services import notifications as note_svc
from analystos.services import reports as report_svc
from analystos.services import schedules as sch_svc

router = APIRouter(prefix="/api", tags=["continuous"])


class ScheduleIn(BaseModel):
    name: str
    kind: str
    cron: str
    timezone: str = "UTC"
    config: dict = {}


class SchedulePatch(BaseModel):
    name: str | None = None
    cron: str | None = None
    timezone: str | None = None
    config: dict | None = None
    enabled: bool | None = None


class MonitorIn(BaseModel):
    name: str
    kind: str
    config: dict = {}
    auto_investigate: bool = False


class MonitorPatch(BaseModel):
    enabled: bool | None = None
    auto_investigate: bool | None = None
    config: dict | None = None
    name: str | None = None


class ReportIn(BaseModel):
    run_id: str | None = None
    kind: str = "executive"
    formats: list[str] = ["html", "pdf", "xlsx"]


class ReadIn(BaseModel):
    ids: list[int]


# ---------------------------------------------------------------------------------- schedules
@router.post("/workspaces/{workspace_id}/schedules")
def create_schedule(workspace_id: str, body: ScheduleIn, user: User = Depends(current_user), session: Session = Depends(db)):
    sch = sch_svc.create_schedule(session, session.merge(user), workspace_id, **body.model_dump())
    session.flush()
    return row(sch)


@router.get("/workspaces/{workspace_id}/schedules")
def list_schedules(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    out = []
    for sch in session.scalars(select(Schedule).where(Schedule.workspace_id == workspace_id).order_by(Schedule.created_at)):
        runs = session.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == sch.id).order_by(ScheduleRun.started_at.desc()).limit(10))
        out.append({**row(sch), "recent_runs": rows(runs)})
    return out


@router.patch("/schedules/{schedule_id}")
def patch_schedule(schedule_id: str, body: SchedulePatch, user: User = Depends(current_user), session: Session = Depends(db)):
    return row(sch_svc.update_schedule(session, session.merge(user), schedule_id, body.model_dump()))


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    sch = session.get(Schedule, schedule_id)
    if sch is None:
        raise NotFound("schedule not found")
    require_role(session, user, sch.workspace_id, "editor")
    audit(f"user:{user.id}", "schedule.deleted", workspace_id=sch.workspace_id, target=sch.id, session=session)
    session.delete(sch)
    return {"deleted": True}


@router.post("/schedules/{schedule_id}/run")
def run_schedule_now(schedule_id: str, user: User = Depends(current_user)):
    srun = sch_svc.run_now(user, schedule_id)
    from analystos.db.base import session_scope

    with session_scope() as s:
        return row(s.get(ScheduleRun, srun))


# ---------------------------------------------------------------------------------- monitors
@router.post("/workspaces/{workspace_id}/monitors")
def create_monitor(workspace_id: str, body: MonitorIn, user: User = Depends(current_user), session: Session = Depends(db)):
    m = mon_svc.create_monitor(session, session.merge(user), workspace_id, **body.model_dump())
    session.flush()
    return row(m)


@router.get("/workspaces/{workspace_id}/monitors")
def list_monitors(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    return rows(session.scalars(select(Monitor).where(Monitor.workspace_id == workspace_id).order_by(Monitor.created_at)))


@router.patch("/monitors/{monitor_id}")
def patch_monitor(monitor_id: str, body: MonitorPatch, user: User = Depends(current_user), session: Session = Depends(db)):
    m = session.get(Monitor, monitor_id)
    if m is None:
        raise NotFound("monitor not found")
    require_role(session, user, m.workspace_id, "editor")
    for k, v in body.model_dump().items():
        if v is not None:
            setattr(m, k, v)
    audit(f"user:{user.id}", "monitor.updated", workspace_id=m.workspace_id, target=m.id, details=body.model_dump(), session=session)
    return row(m)


@router.post("/monitors/{monitor_id}/evaluate")
def evaluate_monitor(monitor_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    m = session.get(Monitor, monitor_id)
    if m is None:
        raise NotFound("monitor not found")
    require_role(session, user, m.workspace_id, "analyst")
    session.commit()
    return mon_svc.evaluate_monitor(monitor_id, trigger=f"user:{user.id}")


@router.get("/monitors/{monitor_id}/series")
def monitor_series(monitor_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    m = session.get(Monitor, monitor_id)
    if m is None or m.kind == "data_quality":
        raise NotFound("metric monitor not found")
    require_role(session, user, m.workspace_id, "viewer")
    owner = session.get(User, m.created_by)
    return mon_svc.metric_series(session, owner, m)


# ---------------------------------------------------------------------------------- alerts
@router.get("/workspaces/{workspace_id}/alerts")
def list_alerts(workspace_id: str, status: str | None = None, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(Alert).where(Alert.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Alert.status == status)
    return rows(session.scalars(stmt.order_by(Alert.created_at.desc()).limit(200)))


@router.post("/alerts/{alert_id}/{action}")
def alert_action(alert_id: str, action: str, user: User = Depends(current_user), session: Session = Depends(db)):
    a = session.get(Alert, alert_id)
    if a is None:
        raise NotFound("alert not found")
    from analystos.decisions.calibration import record_signal

    if action == "investigate":
        require_role(session, user, a.workspace_id, "analyst")
        record_signal(session, "alert.investigate", f"alert:{a.dedupe_key}", user_id=user.id, workspace_id=a.workspace_id)
        session.commit()
        return {"run_id": mon_svc.start_investigation(alert_id, user)}
    require_role(session, user, a.workspace_id, "analyst")
    if action == "acknowledge":
        a.status, a.acknowledged_by = "acknowledged", user.id
        record_signal(session, "alert.acknowledge", f"alert:{a.dedupe_key}", user_id=user.id, workspace_id=a.workspace_id)
    elif action in ("resolve", "dismiss"):
        from analystos.core.ids import utcnow

        a.status, a.resolved_at = "resolved", utcnow()
        if action == "dismiss":  # "not worth attention": the labelled outcome that calibrates alert triage
            a.data = {**(a.data or {}), "dismissed_by": user.id}
            record_signal(session, "alert.dismiss", f"alert:{a.dedupe_key}", user_id=user.id, workspace_id=a.workspace_id)
    else:
        raise NotFound("unknown action")
    audit(f"user:{user.id}", f"alert.{action}", workspace_id=a.workspace_id, target=a.id, session=session)
    return row(a)


# ---------------------------------------------------------------------------------- notifications
@router.get("/notifications")
def notifications(unread: bool = False, user: User = Depends(current_user), session: Session = Depends(db)):
    return [{**row(n), "read": user.id in (n.read_by or [])} for n in note_svc.list_for(session, user, unread_only=unread)]


@router.post("/notifications/read")
def read_notifications(body: ReadIn, user: User = Depends(current_user), session: Session = Depends(db)):
    return {"marked": note_svc.mark_read(session, user, body.ids)}


# ---------------------------------------------------------------------------------- reports
@router.post("/workspaces/{workspace_id}/reports")
def create_report(workspace_id: str, body: ReportIn, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "analyst")
    run_id = body.run_id or session.scalar(select(AnalysisRun.id).where(
        AnalysisRun.workspace_id == workspace_id, AnalysisRun.status == "COMPLETED").order_by(AnalysisRun.finished_at.desc()))
    if not run_id:
        raise InvalidInput("no completed run to report on")
    run = session.get(AnalysisRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise NotFound("run not found in this workspace")
    art = report_svc.generate_report(session, run_id, kind=body.kind, formats=tuple(body.formats), actor=f"user:{user.id}")
    return row(art)


@router.get("/artifacts/{artifact_id}/download")
def download(artifact_id: str, format: str = "pdf", user: User = Depends(current_user), session: Session = Depends(db)):
    """Download to the requesting user (audited). Sending outside the platform would need an approval."""
    art = session.get(Artifact, artifact_id)
    if art is None or art.type != "report":
        raise NotFound("report not found")
    require_role(session, user, art.workspace_id, "viewer")
    content, mime, ext = report_svc.report_file(art, format)
    audit(f"user:{user.id}", "report.downloaded", workspace_id=art.workspace_id, target=art.id, details={"format": format}, session=session)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in art.name)[:60]
    return Response(content, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{safe}.{ext}"'})
