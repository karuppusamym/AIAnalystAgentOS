from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from analystos.core.errors import Unauthenticated
from analystos.core.ids import new_id
from analystos.core.logging import correlation_id
from analystos.db.base import SessionLocal
from analystos.db.models import User
from analystos.security.auth import decode_token


def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_user(authorization: str | None = Header(default=None), session: Session = Depends(db),
                 x_correlation_id: str | None = Header(default=None)) -> User:
    correlation_id.set(x_correlation_id or new_id("req"))
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthenticated("missing bearer token")
    claims = decode_token(authorization.split(" ", 1)[1])
    user = session.get(User, claims["sub"])
    if user is None or not user.active:
        raise Unauthenticated("user inactive or unknown")
    session.expunge(user)
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    from analystos.core.errors import Forbidden

    if not user.is_admin:
        raise Forbidden("platform administrator only")
    return user
