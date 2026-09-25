"""Ask threads (P4-U02): persisted conversations over the governed Ask path.

A turn runs `agents/sql_agent.ask` unchanged in what it may execute (route and clarify decisions,
registry or generation, then gateway validation and scope); this module adds what the UI needs
around it: the thread, the plain-language stages streamed while it runs, one refusal per kind with
a remedy, provenance and staleness, the inspector (decisions and model-call receipts recorded with
`task_id = turn id`), and promotions of an answer into platform objects.

Promotions that stay inside the platform (verified query, metric, monitor, "Investigate why") are
role-checked and validated through the gateway (a metric is proposed to the semantic layer, where an
approver approves it); adding an answer to a dashboard is meant for a BI
audience, so it is an approval bound to the payload hash and runs only after `verify_for_execution`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from analystos.api.serialize import row, rows
from analystos.core.errors import (
    AnalystOSError,
    BudgetExceeded,
    Forbidden,
    InvalidInput,
    ModelRouteUnavailable,
    NotFound,
    PolicyDenied,
    QueryTimeout,
    SQLRejected,
    UpstreamUnavailable,
)
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import (
    Approval,
    Artifact,
    AskThread,
    AskTurn,
    DecisionRecord,
    ModelCall,
    QueryExecution,
    Source,
    SourceAsset,
    User,
)
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, load_policy, require_role, resolve_scope

AGING_AFTER = timedelta(hours=24)
STALE_AFTER = timedelta(days=7)
PROMOTE_TARGETS = ("verified_query", "metric", "monitor", "dashboard", "investigate")
DASHBOARD_ACTION = "ask.add_to_dashboard"
NEW_THREAD_TITLE = "New question"

# One refusal state per kind, each with the remedy the UI shows next to it.
REFUSALS: dict[str, dict[str, str]] = {
    "needs_input": {"title": "A required input is missing",
                    "remedy": "Choose a value for each missing input and ask again. Nothing was guessed."},
    "clarify": {"title": "The question needs more detail",
                "remedy": "Say what to measure (a count, a rate, an average), over which records and period, and how to group it."},
    "sql_rejected": {"title": "The query gateway refused the SQL",
                     "remedy": "Rephrase the question around the tables you can see, or paste the SQL into Explain to see why."},
    "policy_denied": {"title": "Your access does not cover this",
                      "remedy": "Ask a workspace owner for access to this data or to the SQL tool."},
    "budget_exceeded": {"title": "The Ask budget is used up",
                        "remedy": "Wait for the hourly budget to reset, or ask a workspace owner to raise it."},
    "no_model": {"title": "No verified answer, and no model may write SQL here",
                 "remedy": "Write the SQL yourself (Explain checks it first), or save a verified query for this question."},
    "no_scope": {"title": "Nothing in scope to answer from",
                 "remedy": "Add a source, discover it and select its tables in Sources."},
    "timeout": {"title": "The query took too long",
                "remedy": "Narrow the question: a shorter period, fewer groups or a filter."},
    "unavailable": {"title": "A source or model is unavailable right now",
                    "remedy": "Try again in a minute. Nothing was answered from a guess."},
    "failed": {"title": "The question could not be answered",
               "remedy": "Rephrase it, or paste SQL into Explain to check it against the gateway."},
}


def refusal(kind: str, message: str, **details: Any) -> dict[str, Any]:
    spec = REFUSALS[kind]
    return {"kind": kind, "title": spec["title"], "message": message, "remedy": spec["remedy"], "details": details}


def refusal_for(exc: AnalystOSError) -> dict[str, Any]:
    """The refusal kind of a failed Ask, from the error class (not its wording where a class exists)."""
    if isinstance(exc, SQLRejected):
        kind = "sql_rejected"
    elif isinstance(exc, BudgetExceeded):
        kind = "budget_exceeded"
    elif isinstance(exc, (PolicyDenied, Forbidden)):
        kind = "policy_denied"
    elif isinstance(exc, QueryTimeout):
        kind = "timeout"
    elif isinstance(exc, (UpstreamUnavailable, ModelRouteUnavailable)):
        kind = "unavailable"
    elif isinstance(exc, InvalidInput) and "SQL generation unavailable" in exc.message:
        kind = "no_model"
    else:
        kind = "failed"
    return refusal(kind, exc.message, code=exc.code, **({k: v for k, v in exc.details.items() if k in ("scope", "used", "limit")}))


# ------------------------------------------------------------------------------ ad hoc context
@dataclass
class AdhocContext:
    """The SQL agent's context outside a run (Ask, the query console, MCP `ask`). With a `turn_id`
    every model call and decision it makes is recorded against that Ask turn (`task_id`)."""

    user: User
    workspace: Any
    scope: Any
    policy: Any
    agent: Any
    services: Any
    run: Any = None
    task: Any = None
    turn_id: str | None = None
    on_stage: Callable[[str, str, dict[str, Any]], None] | None = None

    @property
    def router(self):
        return self.services.router

    @property
    def jev(self):
        return self.services.jev

    @property
    def subject(self) -> str | None:
        return f"ask:{self.turn_id}" if self.turn_id else None

    def call_ctx(self, exclude_families=None):
        from analystos.llm.router import CallContext

        return CallContext.for_policy(self.policy, workspace_id=self.workspace.id, task_id=self.turn_id, agent_id="sql",
                                      prompt_version="sql.v1", exclude_families=exclude_families or [])

    def say(self, *a, **k):
        return None


def adhoc_context(session: Session, user: User, workspace_id: str) -> AdhocContext:
    from analystos.runtime.context import default_services
    from analystos.tools.registry import get_agent_spec

    scope = resolve_scope(session, session.merge(user), workspace_id)
    ws = get_workspace(session, workspace_id)
    ctx = AdhocContext(user=user, workspace=ws, scope=scope, policy=load_policy(session, ws), agent=get_agent_spec(session, "sql"),
                       services=default_services())
    session.expunge_all()
    return ctx


# ------------------------------------------------------------------------------ threads
def _thread_for(session: Session, user: User, thread_id: str) -> AskThread:
    """Threads are private to their author (a question can reveal intent); the workspace role still applies."""
    thread = session.get(AskThread, thread_id)
    if thread is None or (thread.user_id != user.id and not user.is_admin):
        raise NotFound("thread not found")
    require_role(session, user, thread.workspace_id, "viewer")
    return thread


def _turn_for(session: Session, user: User, turn_id: str) -> AskTurn:
    turn = session.get(AskTurn, turn_id)
    if turn is None:
        raise NotFound("question not found")
    _thread_for(session, user, turn.thread_id)
    return turn


def create_thread(session: Session, user: User, workspace_id: str, title: str | None = None) -> dict[str, Any]:
    require_role(session, user, workspace_id, "viewer")
    get_workspace(session, workspace_id)
    thread = AskThread(id=new_id("ask"), workspace_id=workspace_id, user_id=user.id,
                       title=(title or "").strip()[:300] or NEW_THREAD_TITLE, archived=False)
    session.add(thread)
    session.flush()
    return {**row(thread), "turns": []}


def list_threads(session: Session, user: User, workspace_id: str, q: str | None = None, *, archived: bool = False,
                 limit: int = 50) -> list[dict[str, Any]]:
    """The caller's threads, newest activity first; `q` searches titles and every question asked."""
    require_role(session, user, workspace_id, "viewer")
    stmt = select(AskThread).where(AskThread.workspace_id == workspace_id, AskThread.user_id == user.id,
                                   AskThread.archived.is_(archived))
    if q and q.strip():
        like = f"%{q.strip()}%"
        asked = select(AskTurn.thread_id).where(AskTurn.workspace_id == workspace_id, AskTurn.question.ilike(like))
        stmt = stmt.where(or_(AskThread.title.ilike(like), AskThread.id.in_(asked)))
    threads = list(session.scalars(stmt.order_by(AskThread.updated_at.desc()).limit(min(max(limit, 1), 200))))
    counts = dict(session.execute(select(AskTurn.thread_id, func.count()).where(AskTurn.thread_id.in_([t.id for t in threads]))
                                  .group_by(AskTurn.thread_id)).all()) if threads else {}
    return [{**row(t), "turn_count": counts.get(t.id, 0)} for t in threads]


def thread_detail(session: Session, user: User, thread_id: str) -> dict[str, Any]:
    thread = _thread_for(session, user, thread_id)
    turns = list(session.scalars(select(AskTurn).where(AskTurn.thread_id == thread.id).order_by(AskTurn.seq)))
    return {**row(thread), "turns": [turn_out(session, t) for t in turns]}


def update_thread(session: Session, user: User, thread_id: str, *, title: str | None = None,
                  archived: bool | None = None) -> dict[str, Any]:
    thread = _thread_for(session, user, thread_id)
    if title is not None and title.strip():
        thread.title = title.strip()[:300]
    if archived is not None:
        thread.archived = archived
    session.flush()
    return row(thread)


# ------------------------------------------------------------------------------ provenance and staleness
def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def provenance(session: Session, workspace_id: str, out: dict[str, Any]) -> dict[str, Any]:
    """Where an answer came from: the tables the gateway saw, their source and how fresh they were,
    and which rung answered (verified query, or generated SQL by which model)."""
    result = out.get("result") or {}
    assets = []
    for fq in result.get("referenced_assets") or []:
        schema, _, name = fq.partition(".")
        asset = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                         SourceAsset.schema_name == schema, SourceAsset.name == name))
        source = session.get(Source, asset.source_id) if asset else None
        assets.append({"asset": fq, "asset_id": asset.id if asset else None,
                       "business_name": asset.business_name if asset else None,
                       "source_id": source.id if source else None, "source_name": source.name if source else None,
                       "source_kind": source.kind if source else None,
                       "execution_mode": source.execution_mode if source else None,
                       "freshness_at": _iso(asset.freshness_at) if asset else None,
                       "row_count": asset.row_count if asset else None})
    return {"assets": assets, "answered_by": out.get("answered_by"), "verified_query": out.get("verified_query"),
            "model": out.get("model"), "query_id": result.get("query_id"), "cache_hit": bool(result.get("cache_hit")),
            "result_hash": result.get("result_hash"), "repairs": len(out.get("attempts") or [])}


def staleness(session: Session, turn: AskTurn, now: datetime | None = None) -> dict[str, Any]:
    """How current the answer is, read now: `changed` when a table was refreshed after the answer,
    else by the age of its oldest table (fresh < 24 h <= aging < 7 days <= stale). Unknown stays unknown."""
    assets = [a for a in (turn.provenance or {}).get("assets") or [] if a.get("asset_id")]
    if turn.status != "answered" or not assets:
        return {"state": "unknown", "label": "Data freshness unknown", "data_as_of": None}
    now = now or utcnow()
    current = {a.id: a.freshness_at for a in session.scalars(select(SourceAsset).where(
        SourceAsset.id.in_([a["asset_id"] for a in assets])))}
    recorded = [datetime.fromisoformat(a["freshness_at"]) for a in assets if a.get("freshness_at")]
    if any(current.get(a["asset_id"]) and a.get("freshness_at")
           and current[a["asset_id"]] > datetime.fromisoformat(a["freshness_at"]) for a in assets):
        return {"state": "changed", "label": "Data refreshed since this answer: ask again for current numbers",
                "data_as_of": _iso(min(recorded)) if recorded else None}
    if not recorded:
        return {"state": "unknown", "label": "Data freshness unknown", "data_as_of": None}
    oldest = min(recorded)
    age = now - oldest
    state = "stale" if age >= STALE_AFTER else "aging" if age >= AGING_AFTER else "fresh"
    hours = int(age.total_seconds() // 3600)
    when = f"{hours // 24} days" if hours >= 48 else f"{hours} hours" if hours >= 1 else "under an hour"
    return {"state": state, "label": f"Data as of {when} ago", "data_as_of": _iso(oldest)}


def turn_out(session: Session, turn: AskTurn) -> dict[str, Any]:
    return {**row(turn), "staleness": staleness(session, turn)}


# ------------------------------------------------------------------------------ asking
def _finish(out: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    status = out.get("status")
    if status == "answered":
        return "answered", None
    if status == "needs_input":
        return "needs_input", refusal("needs_input", out.get("explanation") or "", missing=out.get("missing") or [],
                                      verified_query=out.get("verified_query"), parameters=out.get("parameters") or {})
    if status == "clarify":
        return "clarify", refusal("clarify", out.get("explanation") or "", missing=out.get("missing") or [])
    return "refused", out.get("refusal") or refusal("failed", "The question could not be answered.")


def ask_in_thread(user: User, thread_id: str, question: str, parameters: dict[str, Any] | None = None, *,
                  on_stage: Callable[[dict[str, Any]], None] | None = None,
                  ask_fn: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Ask one question in a thread; returns the persisted turn. Errors of the Ask path become the
    turn's refusal (one kind each, with a remedy); only an unknown thread or empty question raise."""
    from analystos.agents.sql_agent import ask as sql_ask

    question = (question or "").strip()
    if not question:
        raise InvalidInput("ask a question")
    started = time.perf_counter()
    with session_scope() as s:
        thread = _thread_for(s, s.merge(user), thread_id)
        seq = (s.scalar(select(func.max(AskTurn.seq)).where(AskTurn.thread_id == thread.id)) or 0) + 1
        turn = AskTurn(id=new_id("askt"), thread_id=thread.id, workspace_id=thread.workspace_id, user_id=user.id, seq=seq,
                       question=question, parameters=dict(parameters or {}), status="running", attempts=[], stages=[],
                       decisions=[], provenance={}, promotions=[])
        s.add(turn)
        if seq == 1 and thread.title == NEW_THREAD_TITLE:
            thread.title = question[:300]
        thread.updated_at = utcnow()
        workspace_id, turn_id = thread.workspace_id, turn.id
    stages: list[dict[str, Any]] = []

    def stage(key: str, text: str, data: dict[str, Any] | None = None) -> None:
        entry = {"key": key, "text": text, "at_ms": int((time.perf_counter() - started) * 1000), **({"data": data} if data else {})}
        stages.append(entry)
        if on_stage is not None:
            on_stage({"turn_id": turn_id, **entry})

    try:
        with session_scope() as s:
            ctx = adhoc_context(s, user, workspace_id)
        ctx.turn_id, ctx.on_stage = turn_id, stage
        if not ctx.scope.assets:
            out = {"status": "refused", "refusal": refusal("no_scope", "No selected, ready tables are in your scope.")}
        else:
            out = (ask_fn or sql_ask)(ctx, question, parameters=parameters)
    except AnalystOSError as exc:
        out = {"status": "refused", "refusal": refusal_for(exc)}
    status, refused = _finish(out)
    stage("done", "Answered" if status == "answered" else (refused or {}).get("title", "Not answered"))
    with session_scope() as s:
        turn = s.get(AskTurn, turn_id)
        turn.status, turn.refusal = status, refused
        turn.route = out.get("route")
        turn.answered_by = out.get("answered_by") if status == "answered" else None
        turn.sql, turn.explanation = out.get("sql"), out.get("explanation")
        turn.chart = out.get("chart") if isinstance(out.get("chart"), dict) else None
        turn.result = out.get("result")
        turn.verified_query = out.get("verified_query")
        turn.model = out.get("model")
        turn.attempts = list(out.get("attempts") or [])
        turn.decisions = list(out.get("decisions") or [])
        turn.provenance = provenance(s, workspace_id, out) if status == "answered" else {}
        turn.stages = stages
        turn.latency_ms = int((time.perf_counter() - started) * 1000)
        emit(workspace_id, "ask.answered", {"thread_id": thread_id, "turn_id": turn_id, "status": status,
                                            "answered_by": turn.answered_by, "refusal": (refused or {}).get("kind")},
             actor=f"user:{user.id}", session=s)
        s.flush()
        return turn_out(s, turn)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def stream_turn(user: User, thread_id: str, question: str, parameters: dict[str, Any] | None = None, *,
                      ask: Callable[..., dict[str, Any]] = ask_in_thread) -> AsyncIterator[str]:
    """SSE frames for one Ask turn: `stage` per plain-language step as it happens, then `turn` (the
    persisted turn) or `error`, then `end`. The Ask itself runs in a worker thread."""
    import anyio

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_stage(entry: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, entry)

    task = asyncio.ensure_future(anyio.to_thread.run_sync(lambda: ask(user, thread_id, question, parameters, on_stage=on_stage)))
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield _sse("stage", getter.result())
                continue
            getter.cancel()
            while not queue.empty():
                yield _sse("stage", queue.get_nowait())
            break
        try:
            yield _sse("turn", task.result())
        except AnalystOSError as exc:
            yield _sse("error", {"error": exc.to_dict()})
        yield _sse("end", {"status": "done"})
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


# ------------------------------------------------------------------------------ inspector
def inspector(session: Session, user: User, turn_id: str) -> dict[str, Any]:
    """The four inspector tabs' data: Result and SQL (the turn and its gateway audit row), Evidence
    (provenance, context receipts), Decision (decision rows and model-call receipts of the turn)."""
    turn = _turn_for(session, user, turn_id)
    decisions = rows(session.scalars(select(DecisionRecord).where(DecisionRecord.task_id == turn.id)
                                     .order_by(DecisionRecord.created_at)))
    calls = list(session.scalars(select(ModelCall).where(ModelCall.task_id == turn.id, ModelCall.workspace_id == turn.workspace_id)
                                 .order_by(ModelCall.id)))
    query_id = (turn.result or {}).get("query_id")
    query = session.get(QueryExecution, query_id) if query_id else None
    receipts: list[dict[str, Any]] = []
    for c in calls:
        for r in c.context_receipts or []:
            if r not in receipts:
                receipts.append(r)
    return {"turn": turn_out(session, turn), "decisions": decisions, "model_calls": rows(calls),
            "query": row(query, exclude={"result_preview"}) if query is not None and query.workspace_id == turn.workspace_id else None,
            "receipts": receipts}


# ------------------------------------------------------------------------------ explain pasted SQL
def explain_sql(session: Session, user: User, workspace_id: str, sql: str, max_rows: int | None = None) -> dict[str, Any]:
    """Paste-SQL explain: the deterministic explanation, the validator's verdict for this caller and,
    when accepted, the source's plan through the gateway (EXPLAIN, nothing executed)."""
    from analystos.gateway.validator import validate_sql
    from analystos.runtime.context import default_gateway
    from analystos.skills.sqlexplain import explain_sql as explain

    scope = resolve_scope(session, session.merge(user), workspace_id)
    dialect = next(iter(scope.source_dialects.values()), "postgres")
    out = explain(sql, dialect)
    try:
        validate_sql(scope, sql, max_rows=max_rows or scope.max_rows)
        out["gateway"] = {"accepted": True}
    except AnalystOSError as exc:
        out["gateway"] = {"accepted": False, "code": exc.code, "reason": exc.message}
        # A rejected probe is audited like a rejected console query: explain must not be a silent scope oracle.
        audit(f"user:{user.id}", "query.explain_rejected", workspace_id=workspace_id, decision="deny",
              details={"code": exc.code, "reason": exc.message[:300], "sql": sql[:2000]}, session=session)
        return out
    try:
        out["plan"] = default_gateway().explain(scope, sql, actor=f"user:{user.id}", purpose="console.explain")
    except AnalystOSError as exc:
        out["plan"] = {"available": False, "reason": exc.message}
    return out


# ------------------------------------------------------------------------------ promotions
def measure_of(sql: str | None, dialect: str = "postgres") -> str | None:
    """The first aggregate in an answer's SELECT, unqualified, as a metric expression (COUNT(*), AVG(x))."""
    import sqlglot
    from sqlglot import exp

    if not sql:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    select_ = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select_ is None:
        return None
    for projection in select_.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(node, exp.AggFunc) or node.find(exp.AggFunc) is not None:
            node = node.copy()
            for col in node.find_all(exp.Column):
                col.set("table", None)
            return node.sql(dialect=dialect)
    return None


def _slug(text: str, n: int = 60) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:n] or "ask"


def _measure(session: Session, user: User, turn: AskTurn, body: dict[str, Any]) -> tuple[str, Artifact, Any]:
    """A metric expression checked through the gateway on the workspace's analytical dataset."""
    from analystos.runtime.context import default_gateway

    scope = resolve_scope(session, user, turn.workspace_id)
    dialect = next(iter(scope.source_dialects.values()), "postgres")
    expression = (body.get("sql_expression") or measure_of(turn.sql, dialect) or "").strip()
    if not expression:
        raise InvalidInput("this answer has no aggregate to measure: give sql_expression (e.g. COUNT(*))")
    if ";" in expression:
        raise InvalidInput("sql_expression is one expression, not a statement")
    dataset = session.scalar(select(Artifact).where(Artifact.workspace_id == turn.workspace_id, Artifact.type == "dataset")
                             .order_by(Artifact.created_at.desc()))
    if dataset is None:
        raise InvalidInput("no analytical dataset in this workspace yet: run an investigation first, then promote")
    sql = f'SELECT {expression} AS value FROM ({dataset.content["sql"]}) d'
    try:
        result = default_gateway().execute(scope, sql, actor=f"user:{user.id}", purpose="metric.validate", max_rows=1)
    except AnalystOSError as exc:
        raise InvalidInput(f"{expression} does not measure the analytical dataset {dataset.name}: {exc.message}") from None
    value = result.rows[0][0] if result.rows else None
    return expression, dataset, {"value": value, "query_id": result.query_id}


def promote(user: User, turn_id: str, target: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn an answered question into a platform object; returns the promotion record."""
    from analystos.artifacts.registry import link, save_artifact
    from analystos.governance.approvals import request_approval, verify_for_execution
    from analystos.registries import verified_queries as vq_svc
    from analystos.services.monitors import create_monitor

    body = dict(body or {})
    if target not in PROMOTE_TARGETS:
        raise InvalidInput(f"target must be one of {', '.join(PROMOTE_TARGETS)}")
    with session_scope() as s:
        me = s.merge(user)
        turn = _turn_for(s, me, turn_id)
        if turn.status != "answered" or not turn.result:
            raise InvalidInput("only an answered question can be promoted")
        ws_id, question, query_id = turn.workspace_id, turn.question, turn.result.get("query_id")
    record: dict[str, Any]
    if target == "investigate":
        from analystos.services.runs import create_run

        objective = (body.get("objective") or f"Investigate why: {question}").strip()
        run = create_run(user, ws_id, objective=objective, origin={"type": "ask", "turn_id": turn_id, "query_id": query_id})
        record = {"target": target, "id": run.id, "status": "started", "objective": objective}
    with session_scope() as s:
        me = s.merge(user)
        turn = _turn_for(s, me, turn_id)
        if target == "verified_query":
            vq = vq_svc.promote(s, me, ws_id, question=body.get("question") or question, query_id=query_id, name=body.get("name"))
            record = {"target": target, "id": vq.id, "name": vq.name, "status": "created"}
        elif target == "metric":
            # A KPI is proposed to the semantic layer (P4-K03): it becomes usable once an approver
            # approves it there (separation of duties); the expression is checked through the gateway first.
            from analystos.contracts.semantic import DialectExpression, SemanticMetricDef
            from analystos.semantic.service import propose_metric

            require_role(s, me, ws_id, "editor")
            expression, dataset, check = _measure(s, me, turn, body)
            name = _slug(body.get("name") or question)
            defn = SemanticMetricDef(name=name, expressions=[DialectExpression(expression=expression)],
                                     description=f"Promoted from Ask: {question}"[:500],
                                     display_name=body.get("display_name") or question[:200], format=body.get("format"),
                                     dataset=dataset.name)
            metric, created = propose_metric(s, ws_id, defn, proposed_by=user.id, via="user", source=("ask_turn", turn_id))
            record = {"target": target, "id": metric.id, "name": metric.name, "version": metric.version,
                      "status": metric.status if not created else "proposed", "approval_id": metric.approval_id,
                      "value": check["value"]}
        elif target == "monitor":
            expression, dataset, check = _measure(s, me, turn, body)
            kind = body.get("kind") or "metric_drift"
            config: dict[str, Any] = {"sql_expression": expression, "dataset_artifact_id": dataset.id,
                                      "grain": body.get("grain") or "week"}
            if kind == "metric_threshold":
                config.update(op=body.get("op"), value=body.get("value"))
            name = (body.get("name") or question)[:200]
            m = create_monitor(s, me, ws_id, name=name, kind=kind, config=config, auto_investigate=bool(body.get("auto_investigate")))
            s.flush()
            record = {"target": target, "id": m.id, "name": m.name, "status": "created", "kind": kind, "value": check["value"]}
        elif target == "dashboard":
            record = _dashboard(s, me, turn, body, request_approval, verify_for_execution, save_artifact, link)
        node = {"investigate": "run", "metric": "semantic_metric",
                "dashboard": "approval" if record["status"] == "approval_required" else "chart"}.get(target, target)
        link(s, ws_id, ("ask_turn", turn_id), "promoted_to", (node, record["id"]))
        record["at"] = utcnow().isoformat()
        turn.promotions = [*(turn.promotions or []), record]
        emit(ws_id, "ask.promoted", {"turn_id": turn_id, **{k: record.get(k) for k in ("target", "id", "status")}},
             actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "ask.promoted", workspace_id=ws_id, target=turn_id,
              details={k: record.get(k) for k in ("target", "id", "status", "approval_id")}, session=s)
    return record


def _dashboard(s: Session, me: User, turn: AskTurn, body: dict[str, Any], request_approval, verify_for_execution,
               save_artifact, link) -> dict[str, Any]:
    """Adding an answer to a dashboard is for a BI audience: an approval over the exact chart payload
    first; with an approved request, verified just before, the chart is added to the dashboard draft."""
    require_role(s, me, turn.workspace_id, "editor")
    ws = get_workspace(s, turn.workspace_id)
    policy = load_policy(s, ws)
    destination = body.get("destination") or (policy.publish_destinations[0] if policy.publish_destinations else None)
    if destination not in policy.publish_destinations:
        raise PolicyDenied(f"destination {destination} is not allowed by this workspace's policy")
    payload = {"kind": "ask_chart", "turn_id": turn.id, "question": turn.question, "sql": turn.sql,
               "chart": body.get("chart") or turn.chart, "dashboard": (body.get("dashboard") or "Ask answers")[:200],
               "destination": destination}
    approval_id = body.get("approval_id")
    if not approval_id:
        apr = request_approval(s, workspace_id=turn.workspace_id, run_id=None, action=DASHBOARD_ACTION, payload=payload,
                               plan_hash=None, policy_version=ws.policy_version, requested_by=me.id, risk_tier="medium",
                               destination=destination, affected_assets=[a["asset"] for a in (turn.provenance or {}).get("assets", [])],
                               evidence={"query_id": (turn.result or {}).get("query_id"), "answered_by": turn.answered_by})
        return {"target": "dashboard", "id": apr.id, "status": "approval_required", "approval_id": apr.id,
                "payload_hash": apr.payload_hash, "dashboard": payload["dashboard"], "destination": destination}
    apr = s.get(Approval, approval_id, with_for_update=True)
    if apr is None or apr.workspace_id != turn.workspace_id or apr.action != DASHBOARD_ACTION:
        raise InvalidInput("the approval does not cover adding this answer to a dashboard")
    verify_for_execution(s, approval_id, payload=payload, plan_hash=None)
    apr.status = "executed"  # single use
    art = save_artifact(s, workspace_id=turn.workspace_id, type_="chart", name=_slug(turn.question), creator_user=me.id,
                        status="approved", content={"title": turn.question[:200], "sql": turn.sql, "chart": payload["chart"],
                                                    "dashboard": payload["dashboard"], "destination": destination,
                                                    "approval_id": approval_id, "origin": {"type": "ask", "turn_id": turn.id}})
    return {"target": "dashboard", "id": art.id, "status": "added", "approval_id": approval_id, "dashboard": payload["dashboard"],
            "destination": destination}
