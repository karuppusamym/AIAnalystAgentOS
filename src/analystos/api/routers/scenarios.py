"""What-if scenarios (N-9): a governed SemanticQuery with declared changes. The observed baseline runs
through the compiler and `QueryGateway`; the scenario is deterministic arithmetic; every number in the
answer is labelled `observed` or `simulated`, and none of it is publishable."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.contracts.scenario import ScenarioSpec
from analystos.db.models import User
from analystos.services import scenarios as svc

router = APIRouter(prefix="/api", tags=["scenarios"])


@router.post("/workspaces/{workspace_id}/scenarios", status_code=201)
def run_scenario(workspace_id: str, body: ScenarioSpec, user: User = Depends(current_user)):
    """Compute and record a scenario: the observed baseline, the simulated values, assumptions and hashes."""
    return svc.run(user, workspace_id, body)


@router.get("/workspaces/{workspace_id}/scenarios")
def list_scenarios(workspace_id: str, ask_turn_id: str | None = Query(default=None, max_length=40),
                   limit: int = Query(default=50, ge=1, le=200), user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    """The caller's own scenarios in this workspace (optionally those made from one Ask answer), newest first."""
    return svc.list_for(session, session.merge(user), workspace_id, ask_turn_id=ask_turn_id, limit=limit)


@router.get("/workspaces/{workspace_id}/scenarios/{scenario_id}")
def get_scenario(workspace_id: str, scenario_id: str, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    return svc.view(svc.load(session, session.merge(user), scenario_id, workspace_id))
