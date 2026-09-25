"""Response cache for model calls whose output depends only on the (redacted) request.

Keyed by purpose + candidate models + request payload hash; stored in Redis with a TTL and an
in-process LRU fallback. Only purposes listed in settings.llm.cacheable_purposes are cached."""
from __future__ import annotations

import contextlib
import json
from collections import OrderedDict
from threading import Lock
from typing import Any

from analystos.core.ids import stable_hash
from analystos.core.logging import get_logger

log = get_logger(__name__)


class ResponseCache:
    def __init__(self, redis_url: str | None = None, *, max_local: int = 2000) -> None:
        self._local: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()
        self._max = max_local
        self._redis = None
        if redis_url:
            try:
                import redis

                self._redis = redis.Redis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
                self._redis.ping()
            except Exception as exc:
                log.warning("llm cache: redis unavailable, using in-process cache (%s)", exc)
                self._redis = None

    @staticmethod
    def key(purpose: str, models: list[str], payload: Any, workspace_id: str | None = None) -> str:
        """Scoped per workspace so its entries can be found and purged (retention, deletion)."""
        return f"aos:llm:{workspace_id or '-'}:{purpose}:{stable_hash({'models': models, 'payload': payload})}"

    def get(self, key: str) -> dict | None:
        raw = None
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
            except Exception:
                raw = None
        if raw is None:
            with self._lock:
                raw = self._local.get(key)
                if raw is not None:
                    self._local.move_to_end(key)
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        raw = json.dumps(value, default=str)
        if self._redis is not None:
            with contextlib.suppress(Exception):
                self._redis.setex(key, ttl_seconds, raw)
        with self._lock:
            self._local[key] = raw
            self._local.move_to_end(key)
            while len(self._local) > self._max:
                self._local.popitem(last=False)


def estimate_tokens(text: str) -> int:
    """Cheap, provider-independent estimate (~3.6 chars/token for English + JSON)."""
    return max(1, int(len(text) / 3.6))
