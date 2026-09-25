"""Decision service in Operate (P4-T08/T09): recent decisions, calibration report, backend overrides,
and the user signals that label decisions (finding accept/dismiss, feedback correction; alert
acknowledge/dismiss live on the alert actions, finding rejection on run feedback)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.api.serialize import rows
from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import DecisionRecord, Feedback, Insight, User
from analystos.decisions import calibration
from analystos.governance.audit import audit
from analystos.governance.policy import require_role

router = APIRouter(prefix="/api", tags=["decisions"])


class CalibrateIn(BaseModel):
    dry_run: bool = False


class BackendStateIn(BaseModel):
    downgraded: bool
    note: str = ""


class FindingOutcomeIn(BaseModel):
    signal: Literal["accept", "dismiss"]


class CorrectionIn(BaseModel):
    kind: str


@router.get("/admin/decisions")
def recent_decisions(purpose: str | None = None, run_id: str | None = None, backend: str | None = None, limit: int = 100,
                     _: User = Depends(admin_user), session: Session = Depends(db)):
    stmt = select(DecisionRecord).order_by(DecisionRecord.created_at.desc()).limit(min(max(limit, 1), 500))
    for col, value in ((DecisionRecord.purpose, purpose), (DecisionRecord.run_id, run_id), (DecisionRecord.backend, backend)):
        if value:
            stmt = stmt.where(col == value)
    return rows(session.scalars(stmt))


@router.get("/admin/decisions/calibration")
def calibration_report(_: User = Depends(admin_user), session: Session = Depends(db)):
    return calibration.report(session)


@router.post("/admin/decisions/calibration/run")
def run_calibration(body: CalibrateIn, admin: User = Depends(admin_user), session: Session = Depends(db)):
    return calibration.run_calibration(session, actor=f"user:{admin.id}", dry_run=body.dry_run)


@router.post("/admin/decisions/backends/{purpose}/{backend}")
def set_backend_state(purpose: str, backend: str, body: BackendStateIn, admin: User = Depends(admin_user),
                      session: Session = Depends(db)):
    return calibration.set_backend_state(session, session.merge(admin), purpose, backend, downgraded=body.downgraded,
                                         note=body.note)


@router.post("/insights/{insight_id}/outcome")
def finding_outcome(insight_id: str, body: FindingOutcomeIn, user: User = Depends(current_user), session: Session = Depends(db)):
    """Accept or dismiss a finding (a rejection goes through run feedback, which also replans)."""
    ins = session.get(Insight, insight_id)
    if ins is None:
        raise NotFound("insight not found")
    require_role(session, user, ins.workspace_id, "analyst")
    labelled = calibration.record_signal(session, f"finding.{body.signal}", f"insight:{ins.id}", user_id=user.id,
                                         workspace_id=ins.workspace_id)
    audit(f"user:{user.id}", f"insight.{body.signal}", workspace_id=ins.workspace_id, run_id=ins.run_id, target=ins.id,
          session=session)
    draft = None
    if body.signal == "accept":  # P4-K08: an accepted, verified finding becomes a draft Attested Computation
        from analystos.knowledge.learning import draft_from_finding

        draft = draft_from_finding(session, ins, user_id=user.id)
    return {"insight": ins.id, "signal": body.signal, "labelled_decisions": labelled,
            "knowledge_draft": draft.id if draft is not None else None}


@router.post("/feedback/{feedback_id}/correct")
def correct_feedback(feedback_id: str, body: CorrectionIn, user: User = Depends(current_user), session: Session = Depends(db)):
    """The user says what kind the feedback really was. Recorded to calibrate feedback_classification;
    submit the feedback again with an explicit kind to act on it."""
    from analystos.services.runs import FEEDBACK_KINDS

    fb = session.get(Feedback, feedback_id)
    if fb is None:
        raise NotFound("feedback not found")
    require_role(session, user, fb.workspace_id, "analyst")
    if body.kind not in FEEDBACK_KINDS:
        raise InvalidInput(f"kind must be one of {sorted(FEEDBACK_KINDS)}")
    fb.data = {**(fb.data or {}), "corrected_kind": body.kind, "corrected_by": user.id}
    labelled = calibration.record_correction(session, "feedback_classification", f"feedback:{fb.id}", body.kind, user_id=user.id,
                                             workspace_id=fb.workspace_id)
    audit(f"user:{user.id}", "feedback.corrected", workspace_id=fb.workspace_id, run_id=fb.run_id, target=fb.id,
          details={"from": fb.kind, "to": body.kind}, session=session)
    return {"feedback": fb.id, "kind": body.kind, "was": fb.kind, "labelled_decisions": labelled}
