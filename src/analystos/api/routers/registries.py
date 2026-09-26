"""Registries API (P4-T05): verified queries for Ask and the hypothesis registry replayed by
scheduled re-analysis."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.core.errors import InvalidInput
from analystos.db.models import RegisteredHypothesis, User, VerifiedQuery
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role
from analystos.registries import verified_queries as vq_svc

router = APIRouter(prefix="/api", tags=["registries"])


class PromoteIn(BaseModel):
    question: str | None = None  # required when promoting an Ask answer (query_id)
    query_id: str | None = None
    insight_id: str | None = None
    name: str | None = None
    description: str = ""
    patterns: list[str] | None = None  # further phrasings of the same question


class VerifiedQueryPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None  # active | retired
    patterns: list[str] | None = None
    parameters: list[dict] | None = None  # [{name, required?, default?}]


class RegisteredHypothesisPatch(BaseModel):
    status: str  # active | retired


@router.get("/workspaces/{workspace_id}/verified-queries")
def list_verified_queries(workspace_id: str, status: str | None = None, user: User = Depends(current_user),
                          session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(VerifiedQuery).where(VerifiedQuery.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(VerifiedQuery.status == status)
    return rows(session.scalars(stmt.order_by(VerifiedQuery.created_at.desc())))


@router.post("/workspaces/{workspace_id}/verified-queries")
def promote_verified_query(workspace_id: str, body: PromoteIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Promote a successful Ask answer (`query_id` + `question`) or a verified finding (`insight_id`)."""
    return row(vq_svc.promote(session, session.merge(user), workspace_id, **body.model_dump()))


@router.patch("/verified-queries/{entry_id}")
def patch_verified_query(entry_id: str, body: VerifiedQueryPatch, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    load_in_workspace(session, VerifiedQuery, entry_id, user=user, label="verified query")
    return row(vq_svc.update(session, session.merge(user), entry_id, body.model_dump(exclude_none=True)))


@router.get("/workspaces/{workspace_id}/hypothesis-registry")
def list_registered_hypotheses(workspace_id: str, status: str | None = None, user: User = Depends(current_user),
                               session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(RegisteredHypothesis).where(RegisteredHypothesis.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(RegisteredHypothesis.status == status)
    return rows(session.scalars(stmt.order_by(RegisteredHypothesis.created_at, RegisteredHypothesis.id)))


@router.patch("/hypothesis-registry/{entry_id}")
def patch_registered_hypothesis(entry_id: str, body: RegisteredHypothesisPatch, user: User = Depends(current_user),
                                session: Session = Depends(db, scope="function")):
    """Retire a question so scheduled re-analysis stops replaying it (or reactivate it)."""
    entry = load_in_workspace(session, RegisteredHypothesis, entry_id, user=user, minimum="analyst",
                              label="registered hypothesis")
    if body.status not in ("active", "retired"):
        raise InvalidInput("status must be active or retired")
    entry.status = body.status
    emit(entry.workspace_id, "hypothesis_registry.updated", {"id": entry.id, "status": entry.status}, session=session)
    audit(f"user:{user.id}", "hypothesis_registry.updated", workspace_id=entry.workspace_id, target=entry.id,
          details={"status": entry.status}, session=session)
    return row(entry)
