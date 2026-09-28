"""What the workspace's data is good for (Data → Overview card): the patterns of the selected tables (event log, time
series, ML candidate, star schema ...) with their evidence and the next step each opens. Deterministic from the catalog;
a person confirms or dismisses a pattern once (kept across crawls). `propose` lets a model suggest an event log for
tables the rules left unexplained; code verifies the mapping against the profile before it is shown."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.base import session_scope
from analystos.db.models import User
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.services import data_shape
from analystos.services.process_tables import allowed_columns, in_scope

router = APIRouter(prefix="/api", tags=["data-shape"])


class ShapeMarkIn(BaseModel):
    asset_id: str
    kind: Literal["event_log", "time_series", "ml_candidate", "fact", "dimension", "bridge", "reference"]
    decision: Literal["confirm", "dismiss", "reset"]


@router.get("/workspaces/{workspace_id}/data-shape")
def get_shape(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The shape of the selected tables the caller may read (their scope: tables and columns)."""
    scope, assets = in_scope(session, user, workspace_id)
    return data_shape.analyze(session, workspace_id, assets=assets, allowed=lambda fq: allowed_columns(scope, fq))


@router.post("/workspaces/{workspace_id}/data-shape/mark")
def mark_pattern(workspace_id: str, body: ShapeMarkIn, user: User = Depends(current_user)):
    """Confirm or dismiss one pattern of one table (editor). A dismissed pattern is not suggested again."""
    with session_scope() as s:
        require_role(s, user, workspace_id, "editor")
        out = data_shape.mark(s, workspace_id, body.asset_id, body.kind, body.decision, user.id)
        audit(f"user:{user.id}", "data_shape.mark", workspace_id=workspace_id, target=body.asset_id,
              decision=body.decision, details={"kind": body.kind}, session=s)
    return out


@router.post("/workspaces/{workspace_id}/data-shape/propose")
def propose_patterns(workspace_id: str, user: User = Depends(current_user)):
    """Ask the model about the tables no rule explained (editor). Verified proposals appear as `proposed` patterns."""
    from analystos.runtime.context import default_router

    with session_scope() as s:
        require_role(s, user, workspace_id, "editor")
        out = data_shape.propose(s, workspace_id, router=default_router())
        audit(f"user:{user.id}", "data_shape.propose", workspace_id=workspace_id,
              details={k: out[k] for k in ("called", "considered", "proposed")}, session=s)
    return out
