from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from analystos.core.errors import Forbidden, NotFound, Unauthenticated
from analystos.core.ids import new_id
from analystos.core.logging import correlation_id
from analystos.db.base import SessionLocal, session_scope
from analystos.db.models import User
from analystos.security.auth import decode_token

if TYPE_CHECKING:
    from analystos.events.stream import StreamGuard


def db() -> Iterator[Session]:
    """Request session, committed before the response is sent: callers declare it with
    `scope="function"` so a 200 means the write is durable (a revoked credential, a decided
    approval) and a failed commit is an error response, not a silent loss after the reply."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _authenticate(session: Session, authorization: str | None, x_correlation_id: str | None) -> User:
    correlation_id.set(x_correlation_id or new_id("req"))
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthenticated("missing bearer token")
    claims = decode_token(authorization.split(" ", 1)[1])
    user = session.get(User, claims["sub"])
    if user is None or not user.active:
        raise Unauthenticated("user inactive or unknown")
    session.expunge(user)
    return user


def current_user(authorization: str | None = Header(default=None), session: Session = Depends(db, scope="function"),
                 x_correlation_id: str | None = Header(default=None)) -> User:
    return _authenticate(session, authorization, x_correlation_id)


@dataclass(frozen=True)
class StreamAuth:
    """The caller of a long-lived response and when its token stops being valid (epoch seconds)."""
    user: User
    expires_at: float | None


async def streaming_auth(authorization: str | None = Header(default=None),
                         x_correlation_id: str | None = Header(default=None)) -> StreamAuth:
    """For long-lived responses (SSE): a `db` dependency lives as long as the response, so one held
    per open stream would exhaust the connection pool. Authenticate in a session closed right away,
    in the same small worker pool the stream reads use (a reconnect storm cannot flood the loop).
    The token's expiry travels with the user: the stream ends when it passes (P4-01)."""
    from analystos.events.stream import run_blocking

    correlation_id.set(x_correlation_id or new_id("req"))

    def authenticate() -> StreamAuth:
        with session_scope() as session:
            user = _authenticate(session, authorization, x_correlation_id)
        exp = decode_token(authorization.split(" ", 1)[1]).get("exp")  # valid: _authenticate decoded it
        return StreamAuth(user=user, expires_at=float(exp) if exp is not None else None)
    return await run_blocking(authenticate)


def stream_guard(auth: StreamAuth, load: Callable[[Session, User], object]) -> StreamGuard:
    """A guard that re-runs the route's own authorization (`load`, e.g. the run in its workspace) for
    this caller in a fresh short session: an inactive user, a lost membership or role, a deleted
    workspace or resource ends the stream."""
    from analystos.events.stream import StreamGuard

    user_id = auth.user.id

    def check() -> str | None:
        with session_scope() as session:
            user = session.get(User, user_id)
            if user is None or not user.active:
                return "user_inactive"
            try:
                load(session, user)
            except (NotFound, Forbidden):
                return "access_revoked"
        return None
    return StreamGuard(check, auth.expires_at)


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise Forbidden("platform administrator only")
    return user
