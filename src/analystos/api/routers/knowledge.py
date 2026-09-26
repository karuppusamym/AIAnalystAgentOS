"""Knowledge API (P4-K05, K07, K08): the review queue for knowledge drafts, a preview of what the
context compiler would put in a prompt, and the context receipts of a run's model calls — the
data the UI's receipts and review screens (P4-U02/U04) render."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.core.errors import InvalidInput
from analystos.db.models import ModelCall, User
from analystos.governance.policy import require_role
from analystos.knowledge import suggestions

router = APIRouter(prefix="/api", tags=["knowledge"])


class ReviewDecision(BaseModel):
    id: str
    action: Literal["approve", "edit", "reject"]
    fields: dict[str, Any] = Field(default_factory=dict)  # edit: field name -> new value
    reason: str | None = Field(None, max_length=2000)


class ReviewIn(BaseModel):
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=200)


class ContextPreviewIn(BaseModel):
    purpose: str = "hypothesis_generation"
    question: str = Field(min_length=1, max_length=4000)


@router.get("/workspaces/{workspace_id}/knowledge/suggestions")
def list_suggestions(workspace_id: str, status: str | None = "pending", kind: str | None = None, origin: str | None = None,
                     limit: int = 100, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The review queue: drafts with per-field value, confidence and provenance."""
    require_role(session, user, workspace_id, "viewer")
    return suggestions.queue(session, workspace_id, status=status or None, kind=kind, origin=origin, limit=limit)


@router.post("/workspaces/{workspace_id}/knowledge/suggestions/review")
def review_suggestions(workspace_id: str, body: ReviewIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Approve, edit-then-approve or reject drafts in one batch: one workspace-pack revision."""
    require_role(session, user, workspace_id, "editor")
    return suggestions.review(session, workspace_id, user, [d.model_dump() for d in body.decisions])


@router.post("/workspaces/{workspace_id}/knowledge/context")
def preview_context(workspace_id: str, body: ContextPreviewIn, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    """What the context compiler would send for `purpose` about `question` from this workspace's
    knowledge (the catalog section is left out: it depends on a run's scope). No model is called."""
    from analystos.context.compiler import KNOWLEDGE_SECTIONS, compile_context, load_knowledge
    from analystos.services.platform_settings import get as platform

    require_role(session, user, workspace_id, "viewer")
    settings = platform()
    profile = settings.context.profiles.get(body.purpose)
    if profile is None:
        raise InvalidInput(f"no context profile for purpose {body.purpose!r}")
    profile = profile.model_copy(update={"sections": [s for s in profile.sections if s in KNOWLEDGE_SECTIONS]})
    items = load_knowledge(session, workspace_id, profile.sections, query=body.question)
    compiled = compile_context(body.purpose, profile, objective=body.question, required={"question": body.question},
                               knowledge=items, min_relevance=settings.context.min_relevance)
    return {**compiled.summary(), "body": compiled.body}


@router.get("/runs/{run_id}/context-receipts")
def run_context_receipts(run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Per model call of a run: the purpose and the receipts of the context it carried."""
    from analystos.services.runs import get_run_for

    run = get_run_for(session, user, run_id, "viewer")
    calls = session.scalars(select(ModelCall).where(ModelCall.run_id == run.id, ModelCall.workspace_id == run.workspace_id)
                            .order_by(ModelCall.id))
    return [{"model_call_id": c.id, "purpose": c.purpose, "agent_id": c.agent_id, "status": c.status,
             "created_at": c.created_at, "receipts": c.context_receipts} for c in calls if c.context_receipts]
