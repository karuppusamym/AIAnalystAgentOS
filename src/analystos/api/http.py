"""HTTP semantics shared by the workbench routes (workbench API §1, P4-06).

* **Idempotency** — `Idempotency-Key` on creates/starts; the service layer decides claim / replay /
  in-progress / 409 (`services/idempotency.py`); a replay carries `Idempotent-Replayed: true`.
* **Revisions** — editable resources return `ETag: "<revision>"`; an edit sends `If-Match`. A stale
  one is 412; a missing one is 428 where the route requires it (new resources). Routes that predate
  revisions accept a missing `If-Match` so existing clients keep working.
* **Cursor pages** — `limit` (default 50, max 200) and an opaque `cursor`, newest first by
  `(created_at, id)`, answered as `{items, next_cursor}`. The cursor is bound to the workspace and
  query it was issued for. A list route that existed before answers its old array when the caller
  sends neither parameter.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from fastapi import Response
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, PreconditionFailed, PreconditionRequired
from analystos.core.ids import stable_hash

DEFAULT_LIMIT, MAX_LIMIT = 50, 200


# ------------------------------------------------------------------------------------ revisions
def etag(revision: int) -> str:
    return f'"{revision}"'


def set_etag(response: Response, revision: int) -> None:
    response.headers["ETag"] = etag(revision)


def expected_revision(if_match: str | None, *, required: bool) -> int | None:
    """The revision an `If-Match` names; None for `*` or (when not required) for no header."""
    if if_match is None or not if_match.strip():
        if required:
            raise PreconditionRequired("send If-Match with the resource's ETag (its revision) to edit it")
        return None
    value = if_match.strip()
    if value == "*":
        return None
    value = value.removeprefix("W/").strip().strip('"')
    try:
        return int(value)
    except ValueError:
        raise PreconditionFailed(f"If-Match {if_match!r} is not a revision of this resource") from None


# ------------------------------------------------------------------------------------ cursor pages
def wants_page(limit: int | None, cursor: str | None) -> bool:
    return limit is not None or cursor is not None


def _binding(scope: dict[str, Any]) -> str:
    return stable_hash(scope)[:16]


def encode_cursor(created_at: datetime, row_id: str, scope: dict[str, Any]) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "id": row_id, "b": _binding(scope)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, scope: dict[str, Any]) -> tuple[datetime, str]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        t, row_id, bound = datetime.fromisoformat(raw["t"]), str(raw["id"]), raw["b"]
    except (binascii.Error, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise InvalidInput("cursor is not valid") from None
    if bound != _binding(scope):
        raise InvalidInput("cursor was issued for a different workspace or query")
    return t, row_id


def page(session: Session, stmt: Any, model: Any, *, limit: int | None, cursor: str | None, scope: dict[str, Any],
         render: Callable[[Sequence[Any]], list[dict[str, Any]]]) -> dict[str, Any]:
    """One page of `stmt` (already filtered and authorized) in stable `(created_at, id)` order, newest
    first. Rows inserted while a client pages appear at the front, never twice and never skipped."""
    n = DEFAULT_LIMIT if limit is None else limit
    if not 1 <= n <= MAX_LIMIT:
        raise InvalidInput(f"limit must be between 1 and {MAX_LIMIT}")
    if cursor:
        t, row_id = decode_cursor(cursor, scope)
        stmt = stmt.where(or_(model.created_at < t, and_(model.created_at == t, model.id < row_id)))
    rows = list(session.scalars(stmt.order_by(model.created_at.desc(), model.id.desc()).limit(n + 1)))
    more = len(rows) > n
    rows = rows[:n]
    return {"items": render(rows),
            "next_cursor": encode_cursor(rows[-1].created_at, rows[-1].id, scope) if more and rows else None}
