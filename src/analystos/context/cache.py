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
from collections.abc import Iterator
from contextvars import ContextVar
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


def entry_key(kind: str, workspace_id: str | None, digest: str) -> str:
    """Every entry lives under `aos:ctx:<workspace>:<kind>:`, so one workspace's entries can be counted and cleared."""
    return f"aos:ctx:{workspace_id or '-'}:{kind}:{digest}"


def key(kind: str, workspace_id: str | None, material: Any) -> str:
    return entry_key(kind, workspace_id, stable_hash(material))


def workspace_of(cache_key: str) -> str | None:
    parts = cache_key.split(":")
    return parts[2] if len(parts) >= 5 and parts[:2] == ["aos", "ctx"] and parts[2] != "-" else None


_QUIET: ContextVar[bool] = ContextVar("aos_context_cache_quiet", default=False)


@contextlib.contextmanager
def unrecorded() -> Iterator[None]:
    """Inspection (the context preview) looks up and fills entries like a real call but is not counted as a
    hit or a miss: the reuse figures stay about the calls that were made."""
    token = _QUIET.set(True)
    try:
        yield
    finally:
        _QUIET.reset(token)


def get(kind: str, purpose: str, cache_key: str) -> Any | None:
    try:
        hit = store().get(cache_key)
    except Exception:
        hit = None
    value = hit.get("v") if isinstance(hit, dict) else None
    note(kind, purpose, "hits" if value is not None else "misses", chars=int(hit.get("chars") or 0) if value is not None else 0,
         workspace_id=workspace_of(cache_key))
    return value


def peek(cache_key: str | None) -> bool:
    """Is there a live entry under this key? Not counted."""
    if not cache_key:
        return False
    try:
        return isinstance(store().get(cache_key), dict)
    except Exception:
        return False


def put(cache_key: str, value: Any, *, chars: int = 0, ttl: int = TTL_SECONDS) -> None:
    try:
        store().set(cache_key, {"v": value, "chars": chars}, ttl)
    except Exception as exc:  # pragma: no cover
        log.warning("context cache: not stored (%s)", exc)


def _workspace_stats_key(workspace_id: str) -> str:
    return f"{STATS_KEY}:ws:{workspace_id}"


def note(kind: str, purpose: str, what: str, *, chars: int = 0, workspace_id: str | None = None) -> None:
    if _QUIET.get():
        return
    try:
        s = store()
        for counter in (STATS_KEY, *([_workspace_stats_key(workspace_id)] if workspace_id else [])):
            s.incr(counter, f"{kind}|{purpose}|{what}")
            if chars:
                s.incr(counter, f"{kind}|{purpose}|chars_reused", chars)
    except Exception:  # pragma: no cover
        pass


def _by_kind(counters: dict[str, float]) -> dict[str, dict[str, dict[str, int]]]:
    out: dict[str, dict[str, dict[str, int]]] = {}
    for field, value in counters.items():
        parts = field.split("|")
        if len(parts) != 3:
            continue
        kind, purpose, what = parts
        out.setdefault(kind, {}).setdefault(purpose, {"hits": 0, "misses": 0, "chars_reused": 0})[what] = int(value)
    return out


def stats() -> dict[str, dict[str, dict[str, int]]]:
    """{kind: {purpose: {hits, misses, chars_reused}}} — shared across workers when Redis is configured."""
    return _by_kind(store().counters(STATS_KEY))


def _workspace_keys(s: ResponseCache, workspace_id: str) -> set[str]:
    """Live entry keys of one workspace: Redis (SCAN, when shared) and this process's LRU (expired ones skipped)."""
    prefix = f"aos:ctx:{workspace_id}:"
    found: set[str] = set()
    redis = getattr(s, "_redis", None)
    if redis is not None:
        with contextlib.suppress(Exception):
            for k in redis.scan_iter(match=f"{prefix}*", count=500):
                found.add(k.decode() if isinstance(k, bytes) else str(k))
    with s._lock:
        now = s._clock()
        found.update(k for k, (expires, _) in s._local.items() if k.startswith(prefix) and expires > now)
    return found


def workspace_stats(workspace_id: str) -> dict[str, Any]:
    """One workspace's shared context cache: live entries per kind, and hits, misses and characters reused per
    kind and purpose since the counters were last cleared (this process only, unless Redis is configured)."""
    s = store()
    entries: dict[str, int] = {}
    for k in _workspace_keys(s, workspace_id):
        kind = k.split(":")[3] if len(k.split(":")) >= 5 else "other"
        entries[kind] = entries.get(kind, 0) + 1
    by_kind = _by_kind(s.counters(_workspace_stats_key(workspace_id)))
    totals = {"hits": 0, "misses": 0, "chars_reused": 0}
    for purposes in by_kind.values():
        for row in purposes.values():
            for k in totals:
                totals[k] += int(row.get(k) or 0)
    return {"workspace_id": workspace_id, "shared": s.shared, "entries": dict(sorted(entries.items())),
            "total_entries": sum(entries.values()), "by_kind": by_kind, "totals": totals}


def clear_workspace(workspace_id: str) -> int:
    """Drop one workspace's entries and counters (Redis and this process). Returns the entries removed. The next
    call compiles and retrieves afresh; nothing else changes."""
    s = store()
    keys = _workspace_keys(s, workspace_id)
    redis = getattr(s, "_redis", None)
    if redis is not None:
        with contextlib.suppress(Exception):
            if keys:
                redis.delete(*keys)
            redis.delete(_workspace_stats_key(workspace_id))
    with s._lock:
        for k in keys:
            s._local.pop(k, None)
        s._counters.pop(_workspace_stats_key(workspace_id), None)
    return len(keys)


def clear() -> None:
    s = store()
    s.clear_local()
    if s.shared:
        with contextlib.suppress(Exception):
            s._redis.delete(STATS_KEY)  # measurement reset only
