"""Workspace brief, readiness and Start-work job kinds (P4-04; shapes in docs/20-contracts/04-brief-steps-api.md).

* `GET/PATCH /brief` — the versioned brief (ETag = version; PATCH needs If-Match), `GET /brief/versions`,
  `POST /brief/suggestions` (deterministic suggestions and checks, never over a person's decision);
* `POST /memory` — workspace-authorized memory, filtered to the caller's scope;
* `GET /capabilities` — the job kinds (Explain, Compare, Forecast, Predict, Prepare data, Monitor) with
  availability and reasons;
* `POST /readiness`, `GET /readiness/{id}`, `POST /work-orders/{id}/assessments` — readiness assessments.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.http import expected_revision, set_etag
from analystos.contracts.brief import BriefPatch, ReadinessIn
from analystos.db.models import ReadinessAssessment, User, WorkOrder
from analystos.governance.policy import load_in_workspace
from analystos.services import brief as brief_svc
from analystos.services import readiness as readiness_svc

router = APIRouter(prefix="/api", tags=["brief"])


class MemoryIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=50)


@router.get("/workspaces/{workspace_id}/brief")
def get_brief(workspace_id: str, response: Response, version: int | None = None, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    out = brief_svc.get(session, session.merge(user), workspace_id, version)
    set_etag(response, out["version"])
    return out


@router.get("/workspaces/{workspace_id}/brief/versions")
def brief_versions(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return brief_svc.history(session, session.merge(user), workspace_id)


@router.patch("/workspaces/{workspace_id}/brief")
def patch_brief(workspace_id: str, body: BriefPatch, response: Response, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function"), if_match: str | None = Header(default=None)):
    """State, review, reject or remove assertions: a new brief version. `If-Match` (the brief version) is required."""
    out = brief_svc.patch(session, session.merge(user), workspace_id, body, expected_revision(if_match, required=True))
    set_etag(response, out["version"])
    return out


@router.post("/workspaces/{workspace_id}/brief/suggestions")
def refresh_brief(workspace_id: str, response: Response, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    out = brief_svc.refresh(session, session.merge(user), workspace_id)
    set_etag(response, out["version"])
    return out


@router.post("/workspaces/{workspace_id}/memory")
def workspace_memory(workspace_id: str, body: MemoryIn, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    return brief_svc.memory(session, user, workspace_id, body.query, limit=body.limit)


@router.get("/workspaces/{workspace_id}/capabilities")
def job_kinds(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Start work: every job kind, whether it can start here for this caller, and why not."""
    from analystos.capabilities import job_kinds as jk
    from analystos.capabilities import registry

    snap = registry.current()
    return {"workspace_id": workspace_id, "digest": snap.digest,
            "job_kinds": jk.availability(session, session.merge(user), workspace_id, snapshot=snap)}


@router.post("/workspaces/{workspace_id}/readiness", status_code=201)
def assess(workspace_id: str, body: ReadinessIn, user: User = Depends(current_user),
           session: Session = Depends(db, scope="function")):
    return readiness_svc.assess(session, session.merge(user), workspace_id, body)


@router.get("/workspaces/{workspace_id}/readiness/{assessment_id}")
def get_assessment(workspace_id: str, assessment_id: str, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    row = load_in_workspace(session, ReadinessAssessment, assessment_id, workspace_id, user=user, label="assessment")
    return readiness_svc.view(row)


@router.post("/workspaces/{workspace_id}/work-orders/{work_order_id}/assessments", status_code=201)
def assess_work_order(workspace_id: str, work_order_id: str, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function")):
    """The work order's current revision against its job kind's readiness checks."""
    wo = load_in_workspace(session, WorkOrder, work_order_id, workspace_id, user=user, minimum="analyst", label="work order")
    return readiness_svc.assess_work_order(session, session.merge(user), wo)
