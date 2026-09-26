"""Budget counters (P4-T07, spec v3 §4.4): run, workspace and per-purpose usage kept in Redis.

The hot path (a budget check before every model call and every run statement) reads and
increments counters instead of running `COUNT(*)` over `query_execution` or `SUM` over
`model_call`. Postgres stays the record: model_call / query_execution rows and the run's
tokens/cost columns are written as before, and a counter that does not exist yet (new run, new
month, Redis flushed) is seeded once from those aggregates with SET NX. Increments only touch
counters that already exist, so a seed always includes every row committed before it.

When Redis is unavailable the caller falls back to the database aggregate and a warning is
logged; budgets are never skipped because the counter store is down.

Hard spend caps (P4-06 "competing budget reservations"): before a billable model call the router
reserves the call's estimated cost against the platform day counter and the workspace month counter
in ONE Lua script (check every cap, then increment every counter), so concurrent calls cannot
overshoot a cap by more than the difference between one call's estimate and its actual cost. After
the call the reservation is settled to the actual cost (a failed call settles to 0 = released). A
reservation cannot be proven without Redis, so it fails closed (SpendCountersUnavailable).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from analystos.core.logging import get_logger

log = get_logger(__name__)

RUN_TTL_SECONDS = 14 * 24 * 3600
MONTH_TTL_SECONDS = 40 * 24 * 3600
DAY_TTL_SECONDS = 3 * 24 * 3600
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


# Reserve `ARGV[1]` on every key or on none. Per key i: ARGV[2i] = cap (negative = no cap), ARGV[2i+1] = TTL.
# Replies: {"seed", i} = key i missing (seed it, retry); {"cap", i, current} = key i would exceed its cap;
# {"ok", new_1, ..., new_n}. RESERVE_SPEND marks the script for test stand-ins.
_RESERVE = """
-- RESERVE_SPEND
local amount = tonumber(ARGV[1])
for i = 1, #KEYS do
  local cur = redis.call('GET', KEYS[i])
  if not cur then return {'seed', tostring(i)} end
  local cap = tonumber(ARGV[2 * i])
  if cap >= 0 and tonumber(cur) + amount > cap + 1e-12 then return {'cap', tostring(i), cur} end
end
local out = {'ok'}
for i = 1, #KEYS do
  table.insert(out, redis.call('INCRBYFLOAT', KEYS[i], amount))
  redis.call('EXPIRE', KEYS[i], ARGV[2 * i + 1])
end
return out
"""


def month_bucket(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m")


def day_bucket(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d")


def day_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


def month_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def next_day_start(now: datetime | None = None) -> datetime:
    return day_start(now) + timedelta(days=1)


def next_month_start(now: datetime | None = None) -> datetime:
    start = month_start(now)
    return (start + timedelta(days=32)).replace(day=1)


@dataclass
class CapSpec:
    """One cap a reservation must fit under. `limit` None = counted, not capped."""

    name: str  # platform_daily | workspace_monthly
    key: str
    limit: float | None
    ttl: int
    seed: Callable[[], float]
    resets_at: datetime | None = None


@dataclass
class SpendReservation:
    """A reserved estimate on each cap's counter; settle() moves it to the actual cost."""

    estimate: float
    caps: list[CapSpec]
    after: dict[str, float] = field(default_factory=dict)  # cap name -> counter value right after reserving
    settled: bool = False


class CapExceeded(Exception):
    def __init__(self, cap: CapSpec, spent: float, estimate: float) -> None:
        super().__init__(cap.name)
        self.cap, self.spent, self.estimate = cap, spent, estimate


class CountersUnavailable(Exception):
    pass


class BudgetCounters:
    def __init__(self, redis_url: str | None, prefix: str = "aos:budget:", *, client: Any = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.prefix = prefix
        self._redis = client
        self._down_until = 0.0
        self._script = None
        self._reserve_script = None
        self.clock = clock or (lambda: datetime.now(UTC))
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
        return self.key("ws", workspace_id, "m", month_bucket(self.clock()), what)

    def platform_day_key(self, what: str = "usd") -> str:
        return self.key("platform", "d", day_bucket(self.clock()), what)

    # ------------------------------------------------------------------ hard caps (reservations)
    def reserve(self, caps: list[CapSpec], estimate: float) -> SpendReservation:
        """Atomically add `estimate` to every cap's counter, or to none when any cap would be exceeded
        (CapExceeded). A missing counter is seeded from the database first (SET NX), then the script
        re-runs. Redis unavailable -> CountersUnavailable: a reservation is never assumed."""
        if not caps:
            return SpendReservation(estimate=0.0, caps=[], settled=True)
        if not self.available:
            raise CountersUnavailable("Redis unavailable")
        amount = max(float(estimate), 0.0)
        try:
            if self._reserve_script is None:
                self._reserve_script = self._redis.register_script(_RESERVE)
            args: list[Any] = [amount]
            for cap in caps:
                args += [-1 if cap.limit is None else float(cap.limit), cap.ttl]
            for _ in range(len(caps) + 2):
                reply = [x.decode() if isinstance(x, bytes) else x for x in self._reserve_script(keys=[c.key for c in caps], args=args)]
                if reply[0] == "ok":
                    return SpendReservation(estimate=amount, caps=caps,
                                            after={c.name: float(v) for c, v in zip(caps, reply[1:], strict=True)})
                cap = caps[int(reply[1]) - 1]
                if reply[0] == "cap":
                    raise CapExceeded(cap, float(reply[2]), amount)
                self._redis.set(cap.key, float(cap.seed()), nx=True, ex=cap.ttl)
            raise CountersUnavailable("spend counters could not be seeded")
        except (CapExceeded, CountersUnavailable):
            raise
        except Exception as exc:
            self._failed(exc)
            raise CountersUnavailable(str(exc)) from exc

    def settle(self, reservation: SpendReservation, actual: float) -> None:
        """Replace the reserved estimate by the actual cost (0 = release). Only counters that still exist
        are adjusted; a re-seeded counter already holds the committed rows. Idempotent per reservation."""
        if reservation.settled:
            return
        reservation.settled = True
        delta = float(actual or 0.0) - reservation.estimate
        if not delta:
            return
        by_ttl: dict[int, dict[str, float]] = {}
        for cap in reservation.caps:
            by_ttl.setdefault(cap.ttl, {})[cap.key] = delta
        for ttl, increments in by_ttl.items():
            self.add(increments, ttl)

    def value(self, key: str) -> float | None:
        """A counter's current value without seeding (None = missing or Redis unavailable)."""
        if not self.available:
            return None
        try:
            raw = self._redis.get(key)
            return None if raw is None else float(raw)
        except Exception as exc:
            self._failed(exc)
            return None

    def mark_once(self, key: str, ttl: int) -> bool:
        """True the first time `key` is marked in its TTL (one alert per cap period across processes)."""
        if not self.available:
            return False
        try:
            return bool(self._redis.set(key, 1, nx=True, ex=ttl))
        except Exception as exc:
            self._failed(exc)
            return False

    # ------------------------------------------------------------------ provider health (admin)
    def note_cooldown(self, provider: str, seconds: float, reason: str) -> None:
        """Share a provider cooldown (e.g. HTTP 402) with the admin health view of other processes."""
        if not self.available:
            return
        try:
            self._redis.set(self.key("provider", provider, "cooldown"), reason[:300], ex=max(int(seconds), 1))
        except Exception as exc:
            self._failed(exc)

    def clear_cooldown(self, provider: str) -> None:
        if not self.available:
            return
        try:
            self._redis.delete(self.key("provider", provider, "cooldown"))
        except Exception as exc:
            self._failed(exc)

    def cooldown(self, provider: str) -> tuple[float, str] | None:
        """(remaining seconds, reason) of a shared provider cooldown, or None."""
        if not self.available:
            return None
        try:
            key = self.key("provider", provider, "cooldown")
            reason, ttl = self._redis.get(key), self._redis.ttl(key)
            if reason is None or ttl is None or int(ttl) <= 0:
                return None
            return float(ttl), reason.decode() if isinstance(reason, bytes) else str(reason)
        except Exception as exc:
            self._failed(exc)
            return None

    def record_model_usage(self, *, workspace_id: str | None, run_id: str | None, purpose: str, tokens: int,
                           cost_usd: float, billable: bool, capped: bool = False) -> None:
        """`capped` = the call held a spend reservation, whose settlement already moved the platform day
        and workspace month counters to the actual cost."""
        if cost_usd and not capped:
            self.add({self.platform_day_key(): cost_usd}, DAY_TTL_SECONDS)
            if workspace_id:
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
