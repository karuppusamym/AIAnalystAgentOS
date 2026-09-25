from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.api.serialize import row
from analystos.core.errors import Conflict, Unauthenticated
from analystos.core.ids import new_id
from analystos.db.models import User
from analystos.governance.audit import audit
from analystos.security.auth import hash_password, issue_token, verify_password

router = APIRouter(prefix="/api", tags=["auth"])


class Login(BaseModel):
    email: str
    password: str


class NewUser(BaseModel):
    email: str
    name: str
    password: str
    is_admin: bool = False
    attributes: dict = {}


@router.post("/auth/login")
def login(body: Login, session: Session = Depends(db)):
    user = session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not user.active or not verify_password(body.password, user.password_hash):
        audit(f"anon:{body.email[:80]}", "auth.login_failed", decision="deny", session=session)
        raise Unauthenticated("invalid email or password")
    audit(f"user:{user.id}", "auth.login", decision="allow", session=session)
    return {"access_token": issue_token(user.id, user.email), "token_type": "bearer",
            "user": row(user, exclude={"password_hash"})}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return row(user, exclude={"password_hash"})


@router.get("/users")
def list_users(_: User = Depends(current_user), session: Session = Depends(db)):
    return [{"id": u.id, "email": u.email, "name": u.name, "is_admin": u.is_admin} for u in session.scalars(select(User))]


@router.post("/users")
def create_user(body: NewUser, admin: User = Depends(admin_user), session: Session = Depends(db)):
    if session.scalar(select(User).where(User.email == body.email.lower())):
        raise Conflict("user exists")
    user = User(id=new_id("usr"), email=body.email.lower(), name=body.name, password_hash=hash_password(body.password),
                is_admin=body.is_admin, attributes=body.attributes)
    session.add(user)
    audit(f"user:{admin.id}", "user.created", target=user.id, session=session)
    return row(user, exclude={"password_hash"})
