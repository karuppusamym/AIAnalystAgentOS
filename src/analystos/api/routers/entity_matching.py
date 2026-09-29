"""General entity matching API (INT-004, N-7). A run proposes links between two tables of the caller's scope
(read through the gateway, compared deterministically, PII hashed); an editor reviews the pairs; promotion is
a hash-bound approval that loads the accepted pairs as the reviewed crosswalk. Every child id is bound to
the path's workspace through a scoped loader."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.models import User
from analystos.services import entity_matching as svc

router = APIRouter(prefix="/api", tags=["entity-matching"])


class PairDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pair_id: str
    decision: Literal["accept", "reject"]
    note: str | None = Field(default=None, max_length=2000)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[PairDecisionIn] = Field(default_factory=list, max_length=5000)
    accept_band: Literal["match", "review"] | None = None  # every still-undecided pair of the band
    reject_band: Literal["match", "review"] | None = None


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str | None = None


@router.post("/workspaces/{workspace_id}/entity-matches", status_code=201)
def start(workspace_id: str, body: dict[str, Any], user: User = Depends(current_user)):
    """Body: a MatchSpec (name, left/right {asset, key, source_id?}, fields [{left, right, type, weight?}],
    match_threshold, review_threshold, one_to_one, max_rows, max_block_size, max_comparisons)."""
    return svc.start_match(user, workspace_id, body)


@router.get("/workspaces/{workspace_id}/entity-matches")
def list_runs(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return [svc.run_view(r) for r in svc.list_runs(session, user, workspace_id)]


@router.get("/workspaces/{workspace_id}/entity-matches/{match_id}")
def get_run(workspace_id: str, match_id: str, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    return svc.run_detail(session, user, match_id, workspace_id)


@router.get("/workspaces/{workspace_id}/entity-matches/{match_id}/pairs")
def list_pairs(workspace_id: str, match_id: str, band: Literal["match", "review"] | None = None,
               decision: Literal["proposed", "accepted", "rejected"] | None = None,
               limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0),
               user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return svc.list_pairs(session, user, match_id, workspace_id, band=band, decision=decision, limit=limit, offset=offset)


@router.post("/workspaces/{workspace_id}/entity-matches/{match_id}/review")
def review(workspace_id: str, match_id: str, body: ReviewIn, user: User = Depends(current_user)):
    return svc.review(user, match_id, workspace_id, decisions=[d.model_dump() for d in body.decisions],
                      accept_band=body.accept_band, reject_band=body.reject_band)


@router.post("/workspaces/{workspace_id}/entity-matches/{match_id}/promote")
def promote(workspace_id: str, match_id: str, body: ApprovalIn, user: User = Depends(current_user)):
    """Without approval_id: request the hash-bound `entity_match.promote` approval (every pair must be decided).
    With it (approved by an approver): load the crosswalk and record the reviewed join keys."""
    return svc.promote(user, match_id, workspace_id, approval_id=body.approval_id)
