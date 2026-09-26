"""`Idempotency-Key` on mutating requests that start work (workbench API §1, P4-06).

A key is scoped by principal, workspace and operation and stored with the canonical hash of the
request body. Resolution: first use → *claim*; the same key and body after completion → *replay*
(the original resource); the same key while the first request still runs → *in progress* (409,
retryable); the same key with a different body → 409 `idempotency_conflict`.

Two ways to hold the claim:

* `transactional` — the claim row is inserted in the same transaction that creates the resource (run
  plus dispatch outbox, schedule, work order). A concurrent duplicate blocks on the unique index until
  the first transaction ends, then replays it (or claims, if it rolled back): no visible in-progress
  state and nothing to clean up after a crash.
* `claim` / `complete` / `release` — for work that spans transactions (an Ask turn): the claim commits
  first with a lease (`locked_until`); a crash leaves it in progress until the lease passes, after which
  a retry with the same body takes it over.

Records are kept `RETENTION` (7 days) and never purged while in progress (`purge_expired`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from analystos.core.errors import IdempotencyConflict, IdempotencyInProgress, InvalidInput
from analystos.core.ids import stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import IdempotencyRecord

RETENTION = timedelta(days=7)
LEASE = timedelta(minutes=10)
MAX_KEY = 200


@dataclass(frozen=True)
class Key:
    principal: str
    workspace_id: str
    operation: str
    key: str
    request_hash: str

    @classmethod
    def of(cls, header: str | None, *, principal: str, workspace_id: str, operation: str, request: Any) -> Key | None:
        """None when the client sent no key (old clients keep today's behaviour)."""
        if header is None:
            return None
        key = header.strip()
        if not key or len(key) > MAX_KEY:
            raise InvalidInput(f"Idempotency-Key must be 1..{MAX_KEY} characters")
        return cls(principal=principal, workspace_id=workspace_id, operation=operation, key=key,
                   request_hash=stable_hash(request))


@dataclass(frozen=True)
class Replay:
    """A completed earlier request with the same key and body."""

    status_code: int
    response: Any
    resource_type: str | None
    resource_id: str | None


def _where(k: Key):
    return (IdempotencyRecord.principal == k.principal, IdempotencyRecord.workspace_id == k.workspace_id,
            IdempotencyRecord.operation == k.operation, IdempotencyRecord.key == k.key)


def _insert(session: Session, k: Key, *, lease: bool) -> bool:
    now = utcnow()
    values = dict(principal=k.principal, workspace_id=k.workspace_id, operation=k.operation, key=k.key,
                  request_hash=k.request_hash, status="in_progress", expires_at=now + RETENTION,
                  locked_until=now + LEASE if lease else None)
    if session.get_bind().dialect.name == "postgresql":
        stmt = pg_insert(IdempotencyRecord).values(**values).on_conflict_do_nothing(
            constraint="uq_idempotency_scope").returning(IdempotencyRecord.id)
        return session.execute(stmt).scalar() is not None
    if session.scalar(select(IdempotencyRecord.id).where(*_where(k))) is not None:  # SQLite (unit tests)
        return False
    session.add(IdempotencyRecord(**values))
    session.flush()
    return True


def _existing(session: Session, k: Key, *, takeover: bool) -> Replay | None:
    """Decide on a record someone else holds; None means this caller may (re)claim it."""
    rec = session.scalar(select(IdempotencyRecord).where(*_where(k)).with_for_update())
    if rec is None:
        return None
    now = utcnow()
    if rec.expires_at is not None and rec.expires_at.replace(tzinfo=rec.expires_at.tzinfo or now.tzinfo) < now:
        session.delete(rec)  # past retention: the key is free again
        session.flush()
        return None
    if rec.request_hash != k.request_hash:
        raise IdempotencyConflict(f"Idempotency-Key {k.key!r} was already used for a different {k.operation} request",
                                  details={"operation": k.operation})
    if rec.status == "completed":
        return Replay(status_code=rec.status_code or 200, response=rec.response, resource_type=rec.resource_type,
                      resource_id=rec.resource_id)
    lease = rec.locked_until.replace(tzinfo=rec.locked_until.tzinfo or now.tzinfo) if rec.locked_until else None
    if takeover and lease is not None and lease < now:
        rec.locked_until = now + LEASE  # the first attempt crashed: this retry takes the claim over
        return None
    raise IdempotencyInProgress(f"a {k.operation} request with this Idempotency-Key is still in progress; retry shortly",
                                details={"operation": k.operation})


def begin_in(session: Session, k: Key) -> Replay | None:
    """Transactional claim inside the caller's transaction: None = claimed, else the replay."""
    if _insert(session, k, lease=False):
        return None
    got = _existing(session, k, takeover=False)
    if got is None:  # expired record removed: claim afresh in this transaction
        _insert(session, k, lease=False)
    return got


def complete_in(session: Session, k: Key, *, response: Any, status_code: int = 200, resource_type: str | None = None,
                resource_id: str | None = None) -> None:
    rec = session.scalar(select(IdempotencyRecord).where(*_where(k)))
    if rec is None:
        return
    rec.status, rec.response, rec.status_code = "completed", response, status_code
    rec.resource_type, rec.resource_id, rec.completed_at, rec.locked_until = resource_type, resource_id, utcnow(), None


def claim(k: Key) -> Replay | None:
    """Claim in its own committed transaction (long work): None = claimed, else the replay."""
    with session_scope() as s:
        if _insert(s, k, lease=True):
            return None
        got = _existing(s, k, takeover=True)
        if got is None and s.scalar(select(IdempotencyRecord.id).where(*_where(k))) is None:
            _insert(s, k, lease=True)
        return got


def complete(k: Key, **kw: Any) -> None:
    with session_scope() as s:
        complete_in(s, k, **kw)


def release(k: Key) -> None:
    """The claimed work failed before producing a resource: free the key so a retry runs it again."""
    with session_scope() as s:
        s.execute(delete(IdempotencyRecord).where(*_where(k), IdempotencyRecord.status == "in_progress"))


def purge_expired(session: Session) -> int:
    res = session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < utcnow(),
                                                          IdempotencyRecord.status == "completed"))
    return int(res.rowcount or 0)
