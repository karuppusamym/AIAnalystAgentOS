"""Per-step execution context. Built fresh for every task execution so that authorization,
policy and scope are re-checked at each step (§12.2): a permission revoked while the run was
paused or waiting for approval blocks the next protected action."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import func, select

from analystos.contracts.policy import DataScope, ExecutionIdentity, WorkspacePolicyDoc
from analystos.contracts.registry import AgentSpec
from analystos.core.config import get_settings
from analystos.core.errors import BudgetExceeded, PolicyDenied, RunCancelled
from analystos.db.base import session_scope
from analystos.db.models import AgentMessage, AnalysisRun, QueryExecution, RunTask, User, Workspace
from analystos.events.bus import emit
from analystos.governance.policy import load_policy, resolve_scope
from analystos.llm.jev import JevDecisions
from analystos.llm.router import CallContext, ModelRouter
from analystos.runtime.usage import DbUsageSink
from analystos.tools.registry import ToolRuntime, get_agent_spec


@lru_cache
def default_router() -> ModelRouter:
    from analystos.llm.cache import ResponseCache

    return ModelRouter(sink=DbUsageSink(), cache=ResponseCache(get_settings().redis_url))


@lru_cache
def default_gateway():
    from analystos.events.bus import emit as _emit
    from analystos.gateway.cache import QueryCache
    from analystos.gateway.service import QueryGateway

    settings = get_settings()

    def on_event(type_: str, payload: dict) -> None:
        ws = payload.get("workspace_id")
        if ws:
            _emit(ws, type_, {k: v for k, v in payload.items() if k in ("query_id", "status", "row_count", "cache_hit",
                                                                         "duration_ms", "reason", "purpose")},
                  run_id=payload.get("run_id"))

    try:
        cache = QueryCache(settings)
    except Exception:
        cache = None
    return QueryGateway(settings, cache=cache, on_event=on_event)


@dataclass
class Services:
    router: ModelRouter
    gateway: Any

    @property
    def jev(self) -> JevDecisions:
        return JevDecisions(self.router)


def default_services() -> Services:
    return Services(router=default_router(), gateway=default_gateway())


@dataclass
class RunContext:
    run: AnalysisRun
    task: RunTask
    user: User
    workspace: Workspace
    policy: WorkspacePolicyDoc
    scope: DataScope
    agent: AgentSpec
    services: Services
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ construction
    @classmethod
    def load(cls, run_id: str, task_key: str, services: Services) -> RunContext:
        with session_scope() as s:
            run = s.get(AnalysisRun, run_id)
            task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == task_key))
            user = s.get(User, run.requested_by)
            workspace = s.get(Workspace, run.workspace_id)
            if user is None or not user.active:
                raise PolicyDenied("run owner is no longer active")
            policy = load_policy(s, workspace)
            # Re-resolve: revocations and policy tightening apply from the next step on.
            scope = resolve_scope(s, user, run.workspace_id, source_ids=run.scope.get("source_ids") or None)
            original = set(run.scope.get("assets") or [])
            scope.assets = [a for a in scope.assets if a in original]  # a run never widens beyond its start scope
            scope.denied_columns = sorted(set(scope.denied_columns) | set(run.scope.get("denied_columns") or []))
            if not scope.assets:
                raise PolicyDenied("no authorized assets remain in scope for this run")
            agent = get_agent_spec(s, task.agent_id)
            s.expunge_all()
        return cls(run=run, task=task, user=user, workspace=workspace, policy=policy, scope=scope, agent=agent, services=services)

    # ------------------------------------------------------------------ helpers
    @property
    def identity(self) -> ExecutionIdentity:
        return ExecutionIdentity(user_id=self.user.id, workspace_id=self.workspace.id, agent_id=self.agent.id,
                                 purpose="analysis", run_id=self.run.id, task_id=self.task.id)

    def call_ctx(self, *, exclude_families: list[str] | None = None) -> CallContext:
        return CallContext(workspace_id=self.workspace.id, run_id=self.run.id, task_id=self.task.id, agent_id=self.agent.id,
                           prompt_version=f"{self.agent.id}.{self.agent.prompt_version}",
                           allowed_models=self.policy.allowed_models, exclude_families=exclude_families or [])

    @property
    def router(self) -> ModelRouter:
        return self.services.router

    @property
    def jev(self) -> JevDecisions:
        return self.services.jev

    def tools(self) -> ToolRuntime:
        return ToolRuntime(user=self.user, identity=self.identity, agent=self.agent)

    def run_sql(self, source_id: str | None = None):
        """Governed SQL runner for skills, with the per-run query budget enforced."""
        gateway = self.services.gateway
        inner = gateway.run_sql_for(self.scope, actor=f"agent:{self.agent.id}", run_id=self.run.id, task_id=self.task.id,
                                    **({"source_id": source_id} if source_id else {}))
        ctx = self

        class _Budgeted:
            dialect = inner.dialect

            def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None):
                ctx.check_control()
                with session_scope() as s:
                    used = s.scalar(select(func.count()).select_from(QueryExecution).where(QueryExecution.run_id == ctx.run.id))
                if used >= ctx.policy.max_queries_per_run:
                    raise BudgetExceeded(f"per-run query budget ({ctx.policy.max_queries_per_run}) exhausted")
                return inner(sql, purpose=purpose, max_rows=max_rows)

        return _Budgeted()

    def check_control(self) -> None:
        with session_scope() as s:
            control, plan_version = s.execute(select(AnalysisRun.control, AnalysisRun.plan_version)
                                              .where(AnalysisRun.id == self.run.id)).one()
        if control == "cancel":
            raise RunCancelled("run cancelled by user")
        if plan_version != self.task.plan_version:
            raise RunCancelled("plan changed while this task was running; result discarded")

    def say(self, content: str, *, kind: str = "observation", data: dict | None = None) -> None:
        with session_scope() as s:
            s.add(AgentMessage(run_id=self.run.id, task_id=self.task.id, agent_id=self.agent.id, kind=kind,
                               content=content[:4000], data=data or {}))
            emit(self.workspace.id, "agent.message", {"agent": self.agent.id, "task": self.task.key, "kind": kind,
                                                       "content": content[:500]}, run_id=self.run.id, session=s)

    def event(self, type_: str, payload: dict) -> None:
        emit(self.workspace.id, type_, payload, run_id=self.run.id, actor=f"agent:{self.agent.id}")
