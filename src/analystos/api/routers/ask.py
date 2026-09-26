"""Ask threads API (P4-U02): threads, turns (JSON or streamed stages over SSE), the answer
inspector, promotions and the paste-SQL explain (on the analysis router)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import StreamAuth, current_user, db, stream_guard, streaming_auth
from analystos.db.base import session_scope
from analystos.db.models import User
from analystos.services import ask as ask_svc

router = APIRouter(prefix="/api", tags=["ask"])


class AskThreadIn(BaseModel):
    title: str | None = None


class AskThreadPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None


class AskTurnIn(BaseModel):
    question: str
    parameters: dict | None = None  # values for a verified query's parameters (answers a "needs_input" refusal)


class AskRerunIn(BaseModel):
    sql: str | None = Field(default=None, max_length=100_000)


class AskScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cron: str = "0 9 * * *"
    timezone: str = "UTC"
    approval_id: str | None = None


class AskPromoteIn(BaseModel):
    target: Literal["verified_query", "metric", "monitor", "dashboard", "investigate"]
    name: str | None = None
    question: str | None = None  # verified query: the phrasing it answers (default: the question asked)
    sql_expression: str | None = None  # metric / monitor: default = the answer's first aggregate
    display_name: str | None = None
    format: str | None = None
    kind: Literal["metric_drift", "metric_threshold", "change_point", "forecast_deviation"] | None = None
    op: str | None = None
    value: float | None = None
    grain: Literal["day", "week", "month"] | None = None
    auto_investigate: bool = False
    dashboard: str | None = None
    destination: str | None = None
    chart: dict | None = None
    objective: str | None = None  # investigate: default "Investigate why: <question>"
    approval_id: str | None = None  # dashboard: an approved request, to add the chart
    extra: dict = Field(default_factory=dict)


@router.get("/workspaces/{workspace_id}/ask/threads")
def list_threads(workspace_id: str, q: str | None = None, archived: bool = False, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    return ask_svc.list_threads(session, user, workspace_id, q, archived=archived)


@router.post("/workspaces/{workspace_id}/ask/threads")
def create_thread(workspace_id: str, body: AskThreadIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return ask_svc.create_thread(session, user, workspace_id, body.title)


@router.get("/ask/threads/{thread_id}")
def get_thread(thread_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return ask_svc.thread_detail(session, user, thread_id)


@router.patch("/ask/threads/{thread_id}")
def patch_thread(thread_id: str, body: AskThreadPatch, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return ask_svc.update_thread(session, user, thread_id, title=body.title, archived=body.archived)


@router.post("/ask/threads/{thread_id}/turns")
async def ask_turn(thread_id: str, body: AskTurnIn, request: Request, auth: StreamAuth = Depends(streaming_auth)):
    """Ask in a thread. With `Accept: text/event-stream` the plain-language stages stream as `stage`
    events, then `turn` (the persisted answer or refusal) and `end`; otherwise the turn is returned.
    A streamed turn ends with `expired` or `revoked` (and nothing after) if the caller loses access."""
    from analystos.events.stream import run_blocking

    user = auth.user

    def load(session: Session, caller: User) -> object:
        return ask_svc._thread_for(session, caller, thread_id)

    def check() -> None:
        with session_scope() as s:
            load(s, s.merge(user))

    await run_blocking(check)  # an unknown thread is a 404 before any stream opens
    if "text/event-stream" in request.headers.get("accept", ""):
        return StreamingResponse(ask_svc.stream_turn(user, thread_id, body.question, body.parameters, guard=stream_guard(auth, load)),
                                 media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    import anyio

    return await anyio.to_thread.run_sync(lambda: ask_svc.ask_in_thread(user, thread_id, body.question, body.parameters))


@router.get("/ask/turns/{turn_id}/inspector")
def inspect_turn(turn_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return ask_svc.inspector(session, user, turn_id)


@router.post("/ask/turns/{turn_id}/rerun")
def rerun(turn_id: str, body: AskRerunIn, user: User = Depends(current_user)):
    return ask_svc.rerun_turn(user, turn_id, body.sql)


@router.post("/ask/turns/{turn_id}/schedule")
def schedule(turn_id: str, body: AskScheduleIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.services.saved_analysis import request_schedule

    return request_schedule(session, user, turn_id, **body.model_dump())


@router.post("/ask/turns/{turn_id}/promote")
def promote_turn(turn_id: str, body: AskPromoteIn, user: User = Depends(current_user)):
    """Promote an answer: verified query, metric, monitor, "Investigate why" (starts a run), or a
    dashboard chart (202 with an approval request first; again with the approved `approval_id`)."""
    fields = body.model_dump(exclude={"target", "extra"}, exclude_none=True)
    out = ask_svc.promote(user, turn_id, body.target, {**body.extra, **fields})
    return JSONResponse(status_code=202, content=out) if out.get("status") == "approval_required" else out
