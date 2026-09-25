"""Model usage sink: every model call is persisted (MOD-003) and counted against budgets (§51).

Budget checks read Redis counters (runtime/budget_counters.py, P4-T07); the database aggregates are
only the seed of a missing counter and the fallback when Redis is unavailable."""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from analystos.core.errors import BudgetExceeded
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, ModelCall, ModelPayload, Workspace
from analystos.governance.policy import load_policy
from analystos.llm.replay import encode_payload
from analystos.llm.router import CallContext
from analystos.runtime import budget_counters
from analystos.runtime.budget_counters import MONTH_TTL_SECONDS, RUN_TTL_SECONDS, BudgetCounters

log = get_logger(__name__)

BILLABLE = ("ok", "error")


def _store_payload(s, kind: str, value: dict | None) -> str | None:
    """Content-addressed insert (idempotent): a retry re-sending the same request stores nothing new."""
    if value is None:
        return None
    digest, body, size, truncated = encode_payload(value)
    s.execute(pg_insert(ModelPayload).values(hash=digest, kind=kind, size_bytes=size, truncated=truncated, body=body)
              .on_conflict_do_nothing(index_elements=["hash"]))
    return digest


def _month_cost_sql(s, workspace_id: str) -> float:
    return float(s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(
        ModelCall.workspace_id == workspace_id, ModelCall.created_at >= func.date_trunc("month", func.now()))) or 0.0)


def _purpose_sql(s, run_id: str, purpose: str, what: str) -> float:
    column = {"calls": func.count().filter(ModelCall.status.in_(BILLABLE)),
              "tokens": func.coalesce(func.sum(ModelCall.input_tokens + ModelCall.output_tokens), 0),
              "usd": func.coalesce(func.sum(ModelCall.cost_usd), 0.0)}[what]
    return float(s.scalar(select(column).where(ModelCall.run_id == run_id, ModelCall.purpose == purpose)) or 0)


class DbUsageSink:
    def __init__(self, counters: BudgetCounters | None = None) -> None:
        self._counters = counters

    @property
    def counters(self) -> BudgetCounters:
        return self._counters if self._counters is not None else budget_counters.default_budget_counters()

    def record(self, *, ctx: CallContext, purpose, profile, provider, model, status, attempt, latency_ms,
               input_tokens, output_tokens, cost_usd, request_hash, error, tokens_saved: int = 0,
               request: dict | None = None, response: dict | None = None, answered_by: str | None = None,
               cost_source: str | None = None) -> None:
        with session_scope() as s:
            s.add(ModelCall(workspace_id=ctx.workspace_id, run_id=ctx.run_id, task_id=ctx.task_id, agent_id=ctx.agent_id,
                            purpose=purpose, profile=profile, provider=provider, model=model, prompt_version=ctx.prompt_version,
                            status=status, attempt=attempt, latency_ms=latency_ms, input_tokens=input_tokens,
                            output_tokens=output_tokens, cost_usd=cost_usd, request_hash=request_hash, error=error,
                            tokens_saved=tokens_saved, request_ref=_store_payload(s, "request", request),
                            response_ref=_store_payload(s, "response", response),
                            answered_by=answered_by or ("rules" if status == "skipped" else "llm_large"),
                            cost_source=cost_source))
            if ctx.run_id and (input_tokens or output_tokens or cost_usd):
                s.execute(update(AnalysisRun).where(AnalysisRun.id == ctx.run_id).values(
                    tokens=AnalysisRun.tokens + input_tokens + output_tokens, cost_usd=AnalysisRun.cost_usd + cost_usd))
        # After the commit, and only on counters that exist: a missing counter is seeded from the
        # database on its next read, which already includes this row. (A seed landing between the
        # commit and this increment counts the call twice: conservative, never under-counted.)
        if status in BILLABLE or cost_usd:
            self.counters.record_model_usage(workspace_id=ctx.workspace_id, run_id=ctx.run_id, purpose=purpose,
                                             tokens=int(input_tokens or 0) + int(output_tokens or 0), cost_usd=float(cost_usd or 0.0),
                                             billable=status in BILLABLE)

    # ------------------------------------------------------------------ budgets
    def _run_usage(self, s, run_id: str) -> tuple[float, float] | None:
        c = self.counters
        run: list[AnalysisRun | None] = []

        def row() -> AnalysisRun | None:
            if not run:
                run.append(s.get(AnalysisRun, run_id))
            return run[0]

        tokens = c.read(c.run_key(run_id, "tokens"), lambda: row().tokens if row() else 0, RUN_TTL_SECONDS)
        usd = c.read(c.run_key(run_id, "usd"), lambda: row().cost_usd if row() else 0.0, RUN_TTL_SECONDS)
        if tokens is None or usd is None:
            if row() is None:
                return None
            return float(row().tokens), float(row().cost_usd)
        return tokens, usd

    def check_budget(self, ctx: CallContext, purpose: str) -> None:
        if not ctx.workspace_id:
            return
        c = self.counters
        with session_scope() as s:
            ws = s.get(Workspace, ctx.workspace_id)
            if ws is None:
                return
            policy = load_policy(s, ws)
            if ctx.run_id:
                usage = self._run_usage(s, ctx.run_id)
                if usage is not None:
                    tokens, usd = usage
                    if usd >= policy.run_cost_budget_usd:
                        raise BudgetExceeded(f"run cost budget ${policy.run_cost_budget_usd:.2f} exhausted")
                    if tokens >= policy.run_token_budget:
                        raise BudgetExceeded(f"run token budget {policy.run_token_budget} exhausted")
                self._check_purpose_caps(s, ctx.run_id, purpose)
            month_cost = c.read(c.workspace_month_key(ctx.workspace_id), lambda: _month_cost_sql(s, ctx.workspace_id),
                                MONTH_TTL_SECONDS)
            if month_cost is None:
                month_cost = _month_cost_sql(s, ctx.workspace_id)
            if month_cost >= policy.workspace_monthly_cost_budget_usd:
                raise BudgetExceeded(f"workspace monthly model budget ${policy.workspace_monthly_cost_budget_usd:.2f} exhausted")

    def _check_purpose_caps(self, s, run_id: str, purpose: str) -> None:
        from analystos.services.platform_settings import get as platform

        caps = platform().llm.purpose_run_caps.get(purpose) or {}
        c = self.counters
        for what, limit in caps.items():
            if limit is None:
                continue
            used = c.read(c.purpose_key(run_id, purpose, what), lambda what=what: _purpose_sql(s, run_id, purpose, what),
                          RUN_TTL_SECONDS)
            if used is None:
                used = _purpose_sql(s, run_id, purpose, what)
            if used >= limit:
                raise BudgetExceeded(f"per-run cap for {purpose} exhausted: {what} {used:g} of {limit:g}",
                                     details={"purpose": purpose, "cap": what, "used": used, "limit": limit})

    def remaining_fraction(self, ctx: CallContext) -> float:
        if not (ctx.workspace_id and ctx.run_id):
            return 1.0
        with session_scope() as s:
            ws = s.get(Workspace, ctx.workspace_id)
            if ws is None:
                return 1.0
            usage = self._run_usage(s, ctx.run_id)
            if usage is None:
                return 1.0
            tokens, usd = usage
            policy = load_policy(s, ws)
            spent = max(usd / policy.run_cost_budget_usd if policy.run_cost_budget_usd else 0,
                        tokens / policy.run_token_budget if policy.run_token_budget else 0)
            return max(0.0, 1.0 - spent)
