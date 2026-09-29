"""Index / partitioning / clustering advice (N-11). Advisory records only: there is no apply route.

* `GET /workspaces/{id}/index-advice` — the advice on assets in the caller's scope (analyst and above);
* `POST /workspaces/{id}/index-advice/analyze` — analyse the audited query history (+ dry plans through the
  gateway) and store the recommendations (editor and above);
* `PATCH /workspaces/{id}/index-advice/{advice_id}` — record a review: open | acknowledged | dismissed.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.models import IndexAdvice, User
from analystos.governance.policy import load_in_workspace
from analystos.services import index_advice as advice_svc

router = APIRouter(prefix="/api", tags=["index-advice"])


class IndexAdviceAnalyzeIn(BaseModel):
    days: int = Field(default=30, ge=1, le=365)
    min_ms: float = Field(default=500, ge=0)
    min_table_rows: int = Field(default=10_000, ge=0)
    explain: bool = True
    max_explains: int = Field(default=10, ge=0, le=50)
    limit: int = Field(default=25, ge=1, le=100)


class IndexAdviceReviewIn(BaseModel):
    status: Literal["open", "acknowledged", "dismissed"]
    note: str | None = Field(default=None, max_length=2000)


@router.get("/workspaces/{workspace_id}/index-advice")
def list_advice(workspace_id: str, status: str | None = None, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    return advice_svc.list_advice(session, user, workspace_id, status)


@router.post("/workspaces/{workspace_id}/index-advice/analyze")
def analyze(workspace_id: str, body: IndexAdviceAnalyzeIn, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    return advice_svc.analyze(session, user, workspace_id, days=body.days, min_ms=body.min_ms,
                              min_table_rows=body.min_table_rows, explain=body.explain,
                              max_explains=body.max_explains, limit=body.limit)


@router.patch("/workspaces/{workspace_id}/index-advice/{advice_id}")
def review(workspace_id: str, advice_id: str, body: IndexAdviceReviewIn, user: User = Depends(current_user),
           session: Session = Depends(db, scope="function")):
    user = session.merge(user)
    row = load_in_workspace(session, IndexAdvice, advice_id, workspace_id, user=user, minimum="editor",
                            label="index advice", for_update=True)
    return advice_svc.review(session, user, row, body.status, body.note)
