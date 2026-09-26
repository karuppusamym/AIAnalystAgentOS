"""SQLAlchemy engines cached per URL (one pool per identity/database; sizes from db/pools.py)."""
from __future__ import annotations

import threading

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from analystos.db.pools import Plane, engine_kwargs

_lock = threading.Lock()
_engines: dict[str, Engine] = {}


def get_engine(url: str, *, plane: Plane = "analytics") -> Engine:
    """Return the cached engine for ``url``, pooled as ``plane`` says when first created."""
    key = make_url(url).render_as_string(hide_password=False)
    engine = _engines.get(key)
    if engine is not None:
        return engine
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = create_engine(key, **engine_kwargs(plane, key))
            _engines[key] = engine
    return engine


def dispose_all() -> None:
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
