from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db, streaming_user
from analystos.api.serialize import row, rows
from analystos.core.errors import InvalidInput, NotFound
from analystos.db.base import session_scope
from analystos.db.models import (
    AgentMessage,
    AnalysisRun,
    Approval,
    Experiment,
    Hypothesis,
    Insight,
    ModelCall,
    QueryExecution,
    RunTask,
    ToolExecution,
    User,
)
from analystos.governance.policy import get_workspace, load_policy, require_role, resolve_scope
from analystos.services import runs as run_svc

router = APIRouter(prefix="/api", tags=["analysis"])


class RunIn(BaseModel):
    objective: str | None = None
    source_ids: list[str] | None = None
    autonomy_level: int | None = None


class FeedbackIn(BaseModel):
    text: str
    kind: str | None = None
    target_type: str | None = None
    target_id: str | None = None


class AskIn(BaseModel):
    question: str


class SqlIn(BaseModel):
    sql: str
    max_rows: int | None = None


class HypothesisPatch(BaseModel):
    statement: str | None = None
    priority: str | None = None
    status: str | None = None


@router.post("/workspaces/{workspace_id}/analysis")
def start(workspace_id: str, body: RunIn, user: User = Depends(current_user)):
    return row(run_svc.create_run(user, workspace_id, **body.model_dump()))


@router.get("/workspaces/{workspace_id}/analysis")
def list_runs(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    return rows(session.scalars(select(AnalysisRun).where(AnalysisRun.workspace_id == workspace_id)
                                .order_by(AnalysisRun.created_at.desc()).limit(50)), exclude={"scope", "plan"})


def _run_detail(session: Session, run: AnalysisRun) -> dict:
    tasks = [t for t in session.scalars(select(RunTask).where(RunTask.run_id == run.id).order_by(RunTask.seq, RunTask.key))
             if not t.key.startswith("_")]
    hyps = list(session.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id).order_by(Hypothesis.created_at)))
    exps = {e.hypothesis_id: e for e in session.scalars(select(Experiment).where(Experiment.run_id == run.id, Experiment.role == "primary"))}
    insights = list(session.scalars(select(Insight).where(Insight.run_id == run.id).order_by(Insight.code)))
    approvals = list(session.scalars(select(Approval).where(Approval.run_id == run.id).order_by(Approval.created_at)))
    scope = dict(run.scope or {})
    return {**row(run), "scope": {k: scope.get(k) for k in ("assets", "denied_columns", "max_rows", "timeout_seconds", "policy_version", "hash")},
            "tasks": rows(tasks),
            "hypotheses": [{**row(h), "result": {k: (exps[h.id].result or {}).get(k) for k in (
                "test", "n", "p_value", "p_adjusted", "effect_size", "effect_label", "highlights", "groups", "warnings")}
                if h.id in exps else None, "experiment_id": exps[h.id].id if h.id in exps else None} for h in hyps],
            "insights": rows(insights), "approvals": rows(approvals, exclude={"payload"})}


@router.get("/workspaces/{workspace_id}/analysis/{run_id}")
def get_run(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    run = run_svc.get_run_for(session, user, run_id)
    if run.workspace_id != workspace_id:
        raise NotFound("run not in workspace")
    return _run_detail(session, run)


@router.get("/agent-runs/{task_id}")
def agent_run(task_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    task = session.get(RunTask, task_id)
    if task is None:
        raise NotFound("agent run not found")
    run_svc.get_run_for(session, user, task.run_id)
    return {**row(task), "messages": rows(session.scalars(select(AgentMessage).where(AgentMessage.task_id == task_id))),
            "tool_calls": rows(session.scalars(select(ToolExecution).where(ToolExecution.task_id == task_id))),
            "model_calls": rows(session.scalars(select(ModelCall).where(ModelCall.task_id == task_id))),
            "queries": rows(session.scalars(select(QueryExecution).where(QueryExecution.task_id == task_id)))}


def _control(run_id: str, action: str, user: User):
    return row(run_svc.control(user, run_id, action), exclude={"scope", "plan"})


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/pause")
def pause(workspace_id: str, run_id: str, user: User = Depends(current_user)):
    return _control(run_id, "pause", user)


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/resume")
def resume(workspace_id: str, run_id: str, user: User = Depends(current_user)):
    return _control(run_id, "resume", user)


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/cancel")
def cancel(workspace_id: str, run_id: str, user: User = Depends(current_user)):
    return _control(run_id, "cancel", user)


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/feedback")
def feedback(workspace_id: str, run_id: str, body: FeedbackIn, user: User = Depends(current_user)):
    return run_svc.submit_feedback(user, run_id, **body.model_dump())


@router.get("/workspaces/{workspace_id}/analysis/{run_id}/console")
def console(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    """Agent console (§52.7): messages, tool calls, model calls, queries, cost."""
    run = run_svc.get_run_for(session, user, run_id)
    calls = list(session.scalars(select(ModelCall).where(ModelCall.run_id == run_id).order_by(ModelCall.id)))
    return {"messages": rows(session.scalars(select(AgentMessage).where(AgentMessage.run_id == run_id).order_by(AgentMessage.id))),
            "tool_calls": rows(session.scalars(select(ToolExecution).where(ToolExecution.run_id == run_id).order_by(ToolExecution.id))),
            "model_calls": rows(calls),
            "queries": rows(session.scalars(select(QueryExecution).where(QueryExecution.run_id == run_id)
                                            .order_by(QueryExecution.created_at)), exclude={"result_preview"}),
            "cost": {"usd": run.cost_usd, "tokens": run.tokens,
                     "model_calls": sum(1 for c in calls if c.status in ("ok", "error")),
                     "jev_calls": sum(1 for c in calls if c.provider == "typesafe" and c.status in ("ok", "error")),
                     "failed_calls": sum(1 for c in calls if c.status == "error"),
                     "cache_hits": sum(1 for c in calls if c.status == "cache_hit"),
                     "deterministic_skips": sum(1 for c in calls if c.status == "skipped"),
                     "tokens_saved": sum(c.tokens_saved or 0 for c in calls)}}


@router.get("/workspaces/{workspace_id}/analysis/{run_id}/events")
async def events(workspace_id: str, run_id: str, request: Request, after_id: int = 0, user: User = Depends(streaming_user)):
    """Persisted event stream as Server-Sent Events. Reconnect with ?after_id=<last id> (or Last-Event-ID).
    Database reads run in worker threads; new events arrive by Redis nudge (polling only as a fallback)."""
    from analystos.events.stream import run_blocking, run_event_stream

    await run_blocking(_authorize_stream, user, workspace_id, run_id)
    last = int(request.headers.get("last-event-id") or after_id)
    return StreamingResponse(run_event_stream(run_id, last, is_disconnected=request.is_disconnected),
                             media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _authorize_stream(user: User, workspace_id: str, run_id: str) -> None:
    with session_scope() as s:
        run = run_svc.get_run_for(s, user, run_id)
        if run.workspace_id != workspace_id:
            raise NotFound("run not in workspace")


@router.patch("/hypotheses/{hypothesis_id}")
def edit_hypothesis(hypothesis_id: str, body: HypothesisPatch, user: User = Depends(current_user), session: Session = Depends(db)):
    h = session.get(Hypothesis, hypothesis_id)
    if h is None:
        raise NotFound("hypothesis not found")
    require_role(session, user, h.workspace_id, "analyst")
    if h.status not in ("proposed", "approved") and body.statement:
        raise InvalidInput("only untested hypotheses can be edited; submit feedback to redirect the analysis instead")
    if body.statement:
        h.statement = body.statement
    if body.priority in ("high", "medium", "low"):
        h.priority = body.priority
    if body.status == "rejected" and h.status in ("proposed", "approved"):
        h.status = "rejected"
        task = session.scalar(select(RunTask).where(RunTask.run_id == h.run_id, RunTask.key == f"test:{h.code}"))
        if task and task.status == "NEW":
            task.status, task.error = "SKIPPED", "rejected by user"
    return row(h)


# --------------------------------------------------------------------------------------- ad hoc
@dataclass
class _AdhocCtx:
    """Minimal context for the SQL agent outside a run (Ask box / query console)."""

    user: User
    workspace: object
    scope: object
    policy: object
    agent: object
    services: object
    run: object = None
    task: object = None

    @property
    def router(self):
        return self.services.router

    @property
    def jev(self):
        return self.services.jev

    def call_ctx(self, exclude_families=None):
        from analystos.llm.router import CallContext

        return CallContext(workspace_id=self.workspace.id, agent_id="sql", prompt_version="sql.v1",
                           allowed_models=self.policy.allowed_models, exclude_families=exclude_families or [])

    def say(self, *a, **k):
        return None


def _adhoc(session: Session, user: User, workspace_id: str) -> _AdhocCtx:
    from analystos.runtime.context import default_services
    from analystos.tools.registry import get_agent_spec

    scope = resolve_scope(session, session.merge(user), workspace_id)
    ws = get_workspace(session, workspace_id)
    ctx = _AdhocCtx(user=user, workspace=ws, scope=scope, policy=load_policy(session, ws), agent=get_agent_spec(session, "sql"),
                    services=default_services())
    session.expunge_all()
    return ctx


@router.post("/workspaces/{workspace_id}/ask")
def ask(workspace_id: str, body: AskIn, user: User = Depends(current_user), session: Session = Depends(db)):
    from analystos.agents.sql_agent import ask as sql_ask

    return sql_ask(_adhoc(session, user, workspace_id), body.question)


@router.post("/workspaces/{workspace_id}/query")
def query(workspace_id: str, body: SqlIn, user: User = Depends(current_user), session: Session = Depends(db)):
    """Manual SQL console: same gateway, same scope and audit as agents (no bypass)."""
    from analystos.runtime.context import default_gateway

    scope = resolve_scope(session, session.merge(user), workspace_id)
    r = default_gateway().execute(scope, body.sql, actor=f"user:{user.id}", purpose="console", max_rows=body.max_rows)
    return r.model_dump()


@router.post("/workspaces/{workspace_id}/query/explain")
def explain_query(workspace_id: str, body: SqlIn, user: User = Depends(current_user), session: Session = Depends(db)):
    """Deterministic explanation (no model, no execution) plus the gateway validator's verdict for this caller."""
    from analystos.core.errors import AnalystOSError
    from analystos.gateway.validator import validate_sql
    from analystos.skills.sqlexplain import explain_sql

    scope = resolve_scope(session, session.merge(user), workspace_id)
    dialect = next(iter(scope.source_dialects.values()), "postgres")
    out = explain_sql(body.sql, dialect)
    try:
        validate_sql(scope, body.sql, max_rows=body.max_rows or scope.max_rows)
        out["gateway"] = {"accepted": True}
    except AnalystOSError as exc:
        out["gateway"] = {"accepted": False, "code": exc.code, "reason": exc.message}
        from analystos.governance.audit import audit

        # A rejected probe is audited like a rejected console query: explain must not be a silent scope oracle.
        audit(f"user:{user.id}", "query.explain_rejected", workspace_id=workspace_id, decision="deny",
              details={"code": exc.code, "reason": exc.message[:300], "sql": body.sql[:2000]}, session=session)
    return out
