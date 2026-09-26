"""Facet-level failure for crawls and knowledge ingestion (P4-K06, spec v3 §6.3).

A crawl is a sequence of facets (profile, relationships, glossary, query history, knowledge
documents, Superset charts, ...). A refused permission or an unreachable endpoint costs the facet it
happened in, never the crawl: the failure is recorded per facet (status, error code, message) and the
remaining facets still run. Only what the facets depend on (connecting, discovery, the catalog diff)
may fail the whole crawl.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from analystos.core.errors import AnalystOSError

log = logging.getLogger(__name__)


class Facets:
    def __init__(self, on_failure: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.results: dict[str, dict[str, Any]] = {}
        self._on_failure = on_failure

    def run(self, name: str, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        """Run one facet; its return value, or None when it failed (the failure is recorded)."""
        started = time.monotonic()
        try:
            out = fn(*args, **kw)
        except AnalystOSError as exc:
            self._fail(name, exc.code, exc.message, started)
            return None
        except Exception as exc:  # noqa: BLE001 - any facet error is contained to its facet
            log.exception("facet %s failed", name)
            self._fail(name, type(exc).__name__, str(exc), started)
            return None
        entry: dict[str, Any] = {"status": "ok", "ms": int((time.monotonic() - started) * 1000)}
        if isinstance(out, dict) and "count" in out:
            entry["count"] = out["count"]
        self.results[name] = entry
        return out

    def skip(self, name: str, reason: str) -> None:
        self.results[name] = {"status": "skipped", "reason": reason}

    def _fail(self, name: str, code: str, message: str, started: float) -> None:
        entry = {"status": "failed", "code": code, "error": str(message)[:500],
                 "ms": int((time.monotonic() - started) * 1000)}
        self.results[name] = entry
        if self._on_failure is not None:
            try:
                self._on_failure(name, entry)
            except Exception:  # noqa: BLE001 - reporting a failure must not raise a second one
                log.exception("facet %s failure could not be reported", name)

    @property
    def failed(self) -> list[str]:
        return [k for k, v in self.results.items() if v["status"] == "failed"]

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return dict(self.results)
