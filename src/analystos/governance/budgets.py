"""Query budgets outside a run. A run is bounded by ``max_queries_per_run`` (RunContext); ad-hoc Ask
has no run, so it is bounded per user and per workspace over a rolling hour, counted from the
gateway's own audit rows (every attempt, rejected ones included, so a repair loop cannot evade it)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.core.errors import BudgetExceeded
from analystos.core.ids import utcnow
from analystos.db.models import QueryExecution

ASK_PURPOSE = "ask"
WINDOW = timedelta(hours=1)


def ask_actor(user_id: str) -> str:
    return f"user:{user_id}"


def check_ask_budget(session: Session, workspace_id: str, user_id: str, policy: WorkspacePolicyDoc) -> dict[str, int]:
    since = utcnow() - WINDOW
    base = select(func.count()).select_from(QueryExecution).where(
        QueryExecution.workspace_id == workspace_id, QueryExecution.purpose == ASK_PURPOSE, QueryExecution.created_at >= since)
    ws_used = session.scalar(base) or 0
    user_used = session.scalar(base.where(QueryExecution.actor == ask_actor(user_id))) or 0
    if user_used >= policy.ask_queries_per_user_per_hour:
        raise BudgetExceeded(f"Ask query budget exhausted: {policy.ask_queries_per_user_per_hour} statements per user per hour",
                             details={"scope": "user", "used": user_used, "limit": policy.ask_queries_per_user_per_hour})
    if ws_used >= policy.ask_queries_per_workspace_per_hour:
        raise BudgetExceeded(
            f"Ask query budget exhausted: {policy.ask_queries_per_workspace_per_hour} statements per workspace per hour",
            details={"scope": "workspace", "used": ws_used, "limit": policy.ask_queries_per_workspace_per_hour})
    return {"user_used": user_used, "workspace_used": ws_used}
