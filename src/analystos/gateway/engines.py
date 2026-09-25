"""SQLAlchemy engines cached per URL (one pool per identity/database)."""
from __future__ import annotations

import threading

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

_lock = threading.Lock()
_engines: dict[str, Engine] = {}


def get_engine(url: str, *, pool_size: int = 5, max_overflow: int = 5) -> Engine:
    """Return the cached engine for ``url`` (created with pool_pre_ping)."""
    key = make_url(url).render_as_string(hide_password=False)
    engine = _engines.get(key)
    if engine is not None:
        return engine
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = create_engine(key, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow)
            _engines[key] = engine
    return engine


def dispose_all() -> None:
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
