"""Model usage sink: every model call is persisted (MOD-003) and counted against budgets (§51)."""
from __future__ import annotations

from sqlalchemy import func, select, update

from analystos.core.errors import BudgetExceeded
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, ModelCall, Workspace
from analystos.governance.policy import load_policy
from analystos.llm.router import CallContext


class DbUsageSink:
    def record(self, *, ctx: CallContext, purpose, profile, provider, model, status, attempt, latency_ms,
               input_tokens, output_tokens, cost_usd, request_hash, error) -> None:
        with session_scope() as s:
            s.add(ModelCall(workspace_id=ctx.workspace_id, run_id=ctx.run_id, task_id=ctx.task_id, agent_id=ctx.agent_id,
                            purpose=purpose, profile=profile, provider=provider, model=model, prompt_version=ctx.prompt_version,
                            status=status, attempt=attempt, latency_ms=latency_ms, input_tokens=input_tokens,
                            output_tokens=output_tokens, cost_usd=cost_usd, request_hash=request_hash, error=error))
            if ctx.run_id and (input_tokens or output_tokens or cost_usd):
                s.execute(update(AnalysisRun).where(AnalysisRun.id == ctx.run_id).values(
                    tokens=AnalysisRun.tokens + input_tokens + output_tokens, cost_usd=AnalysisRun.cost_usd + cost_usd))

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        if not ctx.workspace_id:
            return
        with session_scope() as s:
            ws = s.get(Workspace, ctx.workspace_id)
            if ws is None:
                return
            policy = load_policy(s, ws)
            if ctx.run_id:
                run = s.get(AnalysisRun, ctx.run_id)
                if run and run.cost_usd >= policy.run_cost_budget_usd:
                    raise BudgetExceeded(f"run cost budget ${policy.run_cost_budget_usd:.2f} exhausted")
                if run and run.tokens >= policy.run_token_budget:
                    raise BudgetExceeded(f"run token budget {policy.run_token_budget} exhausted")
            month_cost = s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(
                ModelCall.workspace_id == ctx.workspace_id,
                ModelCall.created_at >= func.date_trunc("month", func.now()))) or 0.0
            if month_cost >= policy.workspace_monthly_cost_budget_usd:
                raise BudgetExceeded(f"workspace monthly model budget ${policy.workspace_monthly_cost_budget_usd:.2f} exhausted")
