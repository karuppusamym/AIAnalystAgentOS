"""Budget counters (P4-T07, spec v3 §4.4): run, workspace and per-purpose usage kept in Redis.

The hot path (a budget check before every model call and every run statement) reads and
increments counters instead of running `COUNT(*)` over `query_execution` or `SUM` over
`model_call`. Postgres stays the record: model_call / query_execution rows and the run's
tokens/cost columns are written as before, and a counter that does not exist yet (new run, new
month, Redis flushed) is seeded once from those aggregates with SET NX. Increments only touch
counters that already exist, so a seed always includes every row committed before it.

When Redis is unavailable the caller falls back to the database aggregate and a warning is
logged; budgets are never skipped because the counter store is down.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from analystos.core.logging import get_logger

log = get_logger(__name__)

RUN_TTL_SECONDS = 14 * 24 * 3600
MONTH_TTL_SECONDS = 40 * 24 * 3600
RETRY_AFTER_SECONDS = 30.0

# Increment only when the counter exists (else the next read seeds it from the database).
_INCR_EXISTING = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  local v = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return v
end
return false
"""


def month_bucket(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m")


class BudgetCounters:
    def __init__(self, redis_url: str | None, prefix: str = "aos:budget:", *, client: Any = None) -> None:
        self.prefix = prefix
        self._redis = client
        self._down_until = 0.0
        self._script = None
        if client is None and redis_url:
            try:
                import redis

                self._redis = redis.Redis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
                self._redis.ping()
            except Exception as exc:
                self._redis = None
                log.warning("budget counters: Redis unavailable (%s); budgets fall back to database aggregates", exc)
        if self._redis is None and not redis_url and client is None:
            log.warning("budget counters: no Redis configured; budgets fall back to database aggregates")

    # ------------------------------------------------------------------ plumbing
    @property
    def available(self) -> bool:
        return self._redis is not None and time.monotonic() >= self._down_until

    def _failed(self, exc: Exception) -> None:
        self._down_until = time.monotonic() + RETRY_AFTER_SECONDS
        log.warning("budget counters: Redis error (%s); falling back to database aggregates for %ss", exc, RETRY_AFTER_SECONDS)

    def key(self, *parts: str) -> str:
        return self.prefix + ":".join(parts)

    def _incr_existing(self, key: str, delta: float, ttl: int) -> float | None:
        if self._script is None:
            self._script = self._redis.register_script(_INCR_EXISTING)
        value = self._script(keys=[key], args=[delta, ttl])
        return float(value) if value is not None else None

    def read(self, key: str, seed: Callable[[], float], ttl: int) -> float | None:
        """The counter, seeded once from the database when missing. None = Redis unavailable."""
        if not self.available:
            return None
        try:
            value = self._redis.get(key)
            if value is None:
                self._redis.set(key, float(seed()), nx=True, ex=ttl)
                value = self._redis.get(key)
            return float(value or 0)
        except Exception as exc:
            self._failed(exc)
            return None

    def add(self, increments: dict[str, float], ttl: int) -> None:
        """Atomically add to counters that exist; missing ones are seeded on their next read."""
        if not self.available or not increments:
            return
        try:
            for key, delta in increments.items():
                if delta:
                    self._incr_existing(key, delta, ttl)
        except Exception as exc:
            self._failed(exc)

    def incr(self, key: str, seed: Callable[[], float], ttl: int) -> float | None:
        """Increment by one and return the new value (seeding first when missing). None = unavailable."""
        if not self.available:
            return None
        try:
            value = self._incr_existing(key, 1, ttl)
            if value is None:
                self._redis.set(key, float(seed()), nx=True, ex=ttl)
                value = float(self._redis.incrbyfloat(key, 1))
            return value
        except Exception as exc:
            self._failed(exc)
            return None

    # ------------------------------------------------------------------ named counters
    def run_key(self, run_id: str, what: str) -> str:
        return self.key("run", run_id, what)

    def purpose_key(self, run_id: str, purpose: str, what: str) -> str:
        return self.key("run", run_id, "p", purpose, what)

    def workspace_month_key(self, workspace_id: str, what: str = "usd") -> str:
        return self.key("ws", workspace_id, "m", month_bucket(), what)

    def record_model_usage(self, *, workspace_id: str | None, run_id: str | None, purpose: str, tokens: int,
                           cost_usd: float, billable: bool) -> None:
        if workspace_id and cost_usd:
            self.add({self.workspace_month_key(workspace_id): cost_usd}, MONTH_TTL_SECONDS)
        if run_id:
            self.add({self.run_key(run_id, "tokens"): tokens, self.run_key(run_id, "usd"): cost_usd,
                      self.purpose_key(run_id, purpose, "tokens"): tokens, self.purpose_key(run_id, purpose, "usd"): cost_usd,
                      self.purpose_key(run_id, purpose, "calls"): 1 if billable else 0}, RUN_TTL_SECONDS)


@lru_cache
def default_budget_counters() -> BudgetCounters:
    from analystos.core.config import get_settings

    settings = get_settings()
    return BudgetCounters(settings.redis_url, settings.budget_counter_prefix)
