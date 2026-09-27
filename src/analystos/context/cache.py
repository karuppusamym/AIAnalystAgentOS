"""Shared context cache (Stream B, CTX-005 extended): compiled prompt context, knowledge retrieval
and the rendered catalog are computed once and reused by every step, worker and Ask turn that asks
for the same thing.

Same store as the L0 response cache (`llm.cache.ResponseCache`): Redis when configured, so a retried
Temporal activity or another worker reuses the entry, else an in-process LRU that honours the TTL.
Every key carries what makes the entry valid: workspace, knowledge version, catalog version, scope
hash, purpose profile and query (callers build the material). Hits and misses are counted per kind
and purpose for the token-savings view; a context hit saves compile and retrieval work, never
provider tokens, so it is never recorded as `tokens_saved`."""
from __future__ import annotations

import contextlib
from typing import Any

from analystos.core.ids import stable_hash
from analystos.core.logging import get_logger
from analystos.llm.cache import ResponseCache

log = get_logger(__name__)

TTL_SECONDS = 900
STATS_KEY = "aos:ctx:stats"
_STORE: ResponseCache | None = None


def store() -> ResponseCache:
    global _STORE
    if _STORE is None:
        try:
            from analystos.core.config import get_settings

            _STORE = ResponseCache(get_settings().redis_url, max_local=512)
        except Exception as exc:  # pragma: no cover - a cache must never break a prompt
            log.warning("context cache: using the in-process store (%s)", exc)
            _STORE = ResponseCache(None, max_local=512)
    return _STORE


def use(new: ResponseCache | None) -> None:
    """Replace the store (tests; None = rebuild from settings on next use)."""
    global _STORE
    _STORE = new


def key(kind: str, workspace_id: str | None, material: Any) -> str:
    return f"aos:ctx:{workspace_id or '-'}:{kind}:{stable_hash(material)}"


def get(kind: str, purpose: str, cache_key: str) -> Any | None:
    try:
        hit = store().get(cache_key)
    except Exception:
        hit = None
    value = hit.get("v") if isinstance(hit, dict) else None
    note(kind, purpose, "hits" if value is not None else "misses", chars=int(hit.get("chars") or 0) if value is not None else 0)
    return value


def put(cache_key: str, value: Any, *, chars: int = 0, ttl: int = TTL_SECONDS) -> None:
    try:
        store().set(cache_key, {"v": value, "chars": chars}, ttl)
    except Exception as exc:  # pragma: no cover
        log.warning("context cache: not stored (%s)", exc)


def note(kind: str, purpose: str, what: str, *, chars: int = 0) -> None:
    try:
        s = store()
        s.incr(STATS_KEY, f"{kind}|{purpose}|{what}")
        if chars:
            s.incr(STATS_KEY, f"{kind}|{purpose}|chars_reused", chars)
    except Exception:  # pragma: no cover
        pass


def stats() -> dict[str, dict[str, dict[str, int]]]:
    """{kind: {purpose: {hits, misses, chars_reused}}} — shared across workers when Redis is configured."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    for field, value in store().counters(STATS_KEY).items():
        parts = field.split("|")
        if len(parts) != 3:
            continue
        kind, purpose, what = parts
        out.setdefault(kind, {}).setdefault(purpose, {"hits": 0, "misses": 0, "chars_reused": 0})[what] = int(value)
    return out


def clear() -> None:
    s = store()
    s.clear_local()
    if s.shared:
        with contextlib.suppress(Exception):
            s._redis.delete(STATS_KEY)  # measurement reset only
