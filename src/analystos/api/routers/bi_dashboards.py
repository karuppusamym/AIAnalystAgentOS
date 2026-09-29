"""Existing-dashboard mode API (v1 §35, BI-011/012): list a BI tool's dashboards this workspace may import,
import one (inspect, map, verify through the gateway, flag, propose), and turn chosen proposals into an
approval-bound update of the BI tool or a semantic metric proposal."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.models import User
from analystos.services import existing_dashboards as svc

router = APIRouter(prefix="/api/workspaces/{workspace_id}/bi/dashboards", tags=["bi"])


class BiDashboardImportIn(BaseModel):
    dashboard_id: str = Field(min_length=1, max_length=200)
    destination: str = "superset"


class BiUpdateRequestIn(BaseModel):
    proposal_ids: list[str] = Field(min_length=1, max_length=50)


class BiUpdateExecuteIn(BaseModel):
    approval_id: str = Field(min_length=1, max_length=40)


class BiMetricProposalIn(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=40)


@router.get("/candidates")
def candidates(workspace_id: str, destination: str = "superset", user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    """Dashboards in the BI tool this workspace may import, with the last import of each."""
    return svc.list_candidates(session, session.merge(user), workspace_id, destination)


@router.get("/imports")
def imports(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return svc.list_imports(session, session.merge(user), workspace_id)


@router.post("/imports")
def import_dashboard(workspace_id: str, body: BiDashboardImportIn, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    """Inspect, map, re-execute every chart through the gateway and compare; changes nothing in the BI tool."""
    return svc.import_dashboard(session, session.merge(user), workspace_id, body.dashboard_id, destination=body.destination)


@router.get("/imports/{import_id}")
def get_import(workspace_id: str, import_id: str, user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    return svc.get_import(session, session.merge(user), workspace_id, import_id)


@router.post("/imports/{import_id}/update-requests")
def request_update(workspace_id: str, import_id: str, body: BiUpdateRequestIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    """Ask for approval to write chosen proposals back to the BI tool (bound to the import's fingerprint)."""
    return svc.request_update(session, session.merge(user), workspace_id, import_id, body.proposal_ids)


@router.post("/imports/{import_id}/updates")
def execute_update(workspace_id: str, import_id: str, body: BiUpdateExecuteIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    """Apply an approved update after re-verifying the approval and the live dashboard's fingerprint."""
    return svc.execute_update(session, session.merge(user), workspace_id, import_id, body.approval_id)


@router.post("/imports/{import_id}/semantic-proposals")
def propose_metric(workspace_id: str, import_id: str, body: BiMetricProposalIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    """Propose an unmapped dashboard metric to the semantic layer (its own approval decides)."""
    return svc.propose_metric(session, session.merge(user), workspace_id, import_id, body.proposal_id)
