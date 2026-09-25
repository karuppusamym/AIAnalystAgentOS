"""Local identity for the MVP (SEC-001 SSO is Phase 4; the token contract is OIDC-shaped so an
IdP can replace the issuer without touching authorization code)."""
from __future__ import annotations

from datetime import timedelta

import bcrypt
import jwt

from analystos.core.config import get_settings
from analystos.core.errors import Unauthenticated
from analystos.core.ids import utcnow

ROLE_RANK = {"viewer": 0, "approver": 1, "analyst": 2, "editor": 3, "owner": 4}
APPROVER_ROLES = {"approver", "owner"}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def issue_token(user_id: str, email: str) -> str:
    settings = get_settings()
    now = utcnow()
    claims = {"sub": user_id, "email": email, "iat": int(now.timestamp()),
              "exp": int((now + timedelta(minutes=settings.jwt_ttl_minutes)).timestamp()), "iss": "analystos"}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"], issuer="analystos")
    except jwt.PyJWTError as exc:
        raise Unauthenticated(f"invalid token: {exc}") from exc


def role_at_least(role: str | None, minimum: str) -> bool:
    return role is not None and ROLE_RANK.get(role, -1) >= ROLE_RANK[minimum]
