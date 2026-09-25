"""Persisted events (§42). Rows are the source of truth; Redis pub/sub is a best-effort nudge."""
from __future__ import annotations

import contextlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import RunEvent

log = get_logger(__name__)
_redis = None


def _redis_client():
    global _redis
    if _redis is None:
        try:
            import redis

            from analystos.core.config import get_settings

            _redis = redis.Redis.from_url(get_settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
        except Exception:  # pragma: no cover
            _redis = False
    return _redis or None


def emit(workspace_id: str, type_: str, payload: dict[str, Any] | None = None, *, run_id: str | None = None,
         actor: str | None = None, session: Session | None = None) -> None:
    event = RunEvent(workspace_id=workspace_id, run_id=run_id, type=type_, payload=payload or {}, actor=actor)
    if session is not None:
        session.add(event)
    else:
        with session_scope() as s:
            s.add(event)
    client = _redis_client()
    if client is not None and run_id:
        with contextlib.suppress(Exception):
            client.publish(f"run:{run_id}", json.dumps({"type": type_}))


def list_events(session: Session, *, workspace_id: str, run_id: str | None = None, after_id: int = 0,
                limit: int = 500) -> list[RunEvent]:
    stmt = select(RunEvent).where(RunEvent.workspace_id == workspace_id, RunEvent.id > after_id)
    if run_id:
        stmt = stmt.where(RunEvent.run_id == run_id)
    return list(session.scalars(stmt.order_by(RunEvent.id).limit(limit)))
