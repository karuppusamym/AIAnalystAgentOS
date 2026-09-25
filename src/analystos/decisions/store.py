"""Where decisions go, and which backends calibration has downgraded.

`DbDecisionStore` writes the `decision` table next to `model_call` (same process, same database)
and reads the effective downgrades from `decision_calibration` with a short cache. A store failure
is logged and never breaks the decision: the caller already has its answer.
`MemoryDecisionStore` is the same interface for tests and for routers without a persisted sink.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Protocol

from analystos.core.logging import get_logger
from analystos.decisions.types import Decision
from analystos.llm.router import CallContext

log = get_logger(__name__)


class DecisionStore(Protocol):
    def record(self, decision: Decision, *, ctx: CallContext | None, options: dict[str, Any]) -> None: ...
    def downgraded(self) -> set[tuple[str, str]]: ...


class MemoryDecisionStore:
    def __init__(self, downgrades: set[tuple[str, str]] | None = None) -> None:
        self.decisions: list[Decision] = []
        self.downgrades = set(downgrades or ())

    def record(self, decision: Decision, *, ctx: CallContext | None, options: dict[str, Any]) -> None:
        self.decisions.append(decision)

    def downgraded(self) -> set[tuple[str, str]]:
        return set(self.downgrades)


_DOWNGRADE_TTL = 60.0
_downgrade_cache: tuple[float, set[tuple[str, str]]] | None = None
_cache_lock = threading.Lock()


def invalidate() -> None:
    global _downgrade_cache
    with _cache_lock:
        _downgrade_cache = None


def load_downgrades() -> set[tuple[str, str]]:
    """(purpose, backend) pairs whose latest calibration row says downgraded."""
    from sqlalchemy import func, select

    from analystos.db.base import session_scope
    from analystos.db.models import DecisionCalibration as Cal

    with session_scope() as s:
        latest = (select(Cal.purpose, Cal.backend, func.max(Cal.id).label("id")).group_by(Cal.purpose, Cal.backend).subquery())
        rows = s.execute(select(Cal.purpose, Cal.backend, Cal.downgraded).join(latest, Cal.id == latest.c.id)).all()
    return {(p, b) for p, b, down in rows if down}


class DbDecisionStore:
    def record(self, decision: Decision, *, ctx: CallContext | None, options: dict[str, Any]) -> None:
        from analystos.db.base import session_scope
        from analystos.db.models import DecisionRecord

        ctx = ctx or CallContext()
        try:
            with session_scope() as s:
                s.add(DecisionRecord(
                    id=decision.id, workspace_id=ctx.workspace_id, run_id=ctx.run_id, task_id=ctx.task_id, agent_id=ctx.agent_id,
                    purpose=decision.purpose, authority=decision.authority, backend=decision.backend, model=decision.model,
                    inputs_hash=decision.inputs_hash, subject=decision.subject, options=options, answer=decision.value,
                    proposal=decision.proposal, probabilities=decision.probabilities, confidence=decision.confidence,
                    latency_ms=decision.latency_ms, cost_usd=decision.cost_usd, fallback_reason=decision.fallback_reason,
                    attempts=decision.attempts, enforced=decision.enforced))
        except Exception as exc:  # noqa: BLE001 - recording must never break the decision itself
            log.warning("decision %s (%s) not recorded: %s", decision.id, decision.purpose, str(exc).splitlines()[0][:200])

    def downgraded(self) -> set[tuple[str, str]]:
        global _downgrade_cache
        with _cache_lock:
            if _downgrade_cache and time.monotonic() - _downgrade_cache[0] < _DOWNGRADE_TTL:
                return set(_downgrade_cache[1])
        try:
            value = load_downgrades()
        except Exception as exc:  # noqa: BLE001 - no calibration state = the configured order
            log.warning("decision downgrades unavailable: %s", str(exc).splitlines()[0][:200])
            value = set()
        with _cache_lock:
            _downgrade_cache = (time.monotonic(), value)
        return set(value)
