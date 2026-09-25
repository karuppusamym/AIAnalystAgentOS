"""In-app notifications. Delivery outside the platform (email, chat, webhooks) is an external
side effect and would go through an approval (§39); it is not part of this release."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.db.models import Notification, User, WorkspaceMember
from analystos.events.bus import emit


def notify(session: Session, workspace_id: str, *, kind: str, title: str, body: str = "", link: dict | None = None,
           user_id: str | None = None) -> Notification:
    n = Notification(workspace_id=workspace_id, user_id=user_id, kind=kind, title=title[:300], body=body[:4000],
                     link=link or {}, read_by=[])
    session.add(n)
    emit(workspace_id, "notification.created", {"kind": kind, "title": title[:200]}, session=session)
    return n


def list_for(session: Session, user: User, *, unread_only: bool = False, limit: int = 100) -> list[Notification]:
    workspaces = select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
    stmt = select(Notification).where(or_(Notification.user_id == user.id, Notification.user_id.is_(None)))
    if not user.is_admin:
        stmt = stmt.where(Notification.workspace_id.in_(workspaces))
    rows = list(session.scalars(stmt.order_by(Notification.id.desc()).limit(limit)))
    return [n for n in rows if not (unread_only and user.id in (n.read_by or []))]


def mark_read(session: Session, user: User, ids: list[int]) -> int:
    count = 0
    for n in session.scalars(select(Notification).where(Notification.id.in_(ids))):
        if user.id not in (n.read_by or []):
            n.read_by = [*(n.read_by or []), user.id]
            count += 1
    return count
