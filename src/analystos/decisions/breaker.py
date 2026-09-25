"""Circuit breaker per backend (process-wide): an outage costs N failed calls, not one per decision.

closed    calls pass; `failures` consecutive failures open the circuit
open      calls are refused until `reset_seconds` have passed
half_open one probe call passes; success closes, failure re-opens
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CircuitBreaker:
    name: str
    failures: int = 3
    reset_seconds: float = 30.0
    state: str = "closed"
    consecutive: int = 0
    opened_at: float = 0.0
    probing: bool = False
    last_error: str | None = None
    clock: Any = field(default=time.monotonic, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def allow(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open" and self.clock() - self.opened_at >= self.reset_seconds:
                self.state, self.probing = "half_open", False
            if self.state == "half_open" and not self.probing:
                self.probing = True
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self.state, self.consecutive, self.probing, self.last_error = "closed", 0, False, None

    def failure(self, error: str) -> None:
        with self._lock:
            self.last_error = error[:300]
            self.consecutive += 1
            if self.state == "half_open" or self.consecutive >= self.failures:
                self.state, self.opened_at, self.probing = "open", self.clock(), False

    def release(self) -> None:
        """A probe that neither succeeded nor failed (the backend abstained): let the next call probe."""
        with self._lock:
            self.probing = False

    def snapshot(self) -> dict[str, Any]:
        return {"backend": self.name, "state": self.state, "consecutive_failures": self.consecutive,
                "failures_to_open": self.failures, "reset_seconds": self.reset_seconds, "last_error": self.last_error}


_BREAKERS: dict[str, CircuitBreaker] = {}
_REG_LOCK = threading.Lock()


def breaker(name: str, *, failures: int = 3, reset_seconds: float = 30.0) -> CircuitBreaker:
    with _REG_LOCK:
        b = _BREAKERS.get(name)
        if b is None:
            b = _BREAKERS[name] = CircuitBreaker(name, failures=failures, reset_seconds=reset_seconds)
        else:
            b.failures, b.reset_seconds = failures, reset_seconds
        return b


def snapshot() -> list[dict[str, Any]]:
    with _REG_LOCK:
        return [b.snapshot() for b in _BREAKERS.values()]


def reset_all() -> None:
    with _REG_LOCK:
        _BREAKERS.clear()
