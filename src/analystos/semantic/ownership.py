"""Ownership transfer of a governed metric or the workspace semantic model (P4-05, SEM-005).

Two people, one hash-bound record. The current owner (or a workspace owner) *offers* the definition to a named
member; that creates an approval whose payload binds the subject, both owners and the content hash of the
version being handed over. Only the named new owner can *accept* it, and acceptance re-derives the payload from
the definition as it is now: if the definition changed, was re-owned or the policy moved since the offer, the
payload hash no longer matches and acceptance is refused (a new offer is needed). The generic approvals inbox
cannot approve an offer (an approver is not the recipient). Ownership covers every version of the name, and new
versions inherit it.
"""
from __future__ import annotations

from datetime import UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import stable_hash, utcnow
from analystos.db.models import Approval, SemanticMetric, SemanticModel, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, member_role, require_role
from analystos.security.auth import role_at_least

ACTION = "semantic.ownership_transfer"
SUBJECTS = {"metric": SemanticMetric, "model": SemanticModel}
LIVE = ("proposed", "approved", "draft")


def _rows(session: Session, workspace_id: str, subject: str, name: str) -> list[Any]:
    model = SUBJECTS[subject]
    return list(session.scalars(select(model).where(model.workspace_id == workspace_id, model.name == name)
                                .order_by(model.version)))


def _current(rows: list[Any]) -> Any:
    """The version whose ownership is offered: the approved one, else the newest live one."""
    approved = [r for r in rows if r.status == "approved"]
    live = [r for r in rows if r.status in LIVE]
    return (approved or live or [None])[-1]


def payload(session: Session, workspace_id: str, subject: str, name: str, to_owner: str) -> dict[str, Any]:
    if subject not in SUBJECTS:
        raise InvalidInput(f"subject must be one of {sorted(SUBJECTS)}")
    rows = _rows(session, workspace_id, subject, name)
    cur = _current(rows)
    if cur is None:
        raise NotFound(f"{subject} {name} not found")
    return {"action": ACTION, "workspace_id": workspace_id, "subject": subject, "name": name, "version": cur.version,
            "content_hash": cur.content_hash, "from_owner": cur.owner_id, "to_owner": to_owner}


def offer(session: Session, user: User, workspace_id: str, subject: str, name: str, *, to_owner: str,
          reason: str | None = None) -> Approval:
    """The current owner (or a workspace owner) offers the definition to a member who can edit it."""
    from analystos.governance.approvals import request_approval

    role = require_role(session, user, workspace_id, "editor")
    body = payload(session, workspace_id, subject, name, to_owner)
    if body["from_owner"] != user.id and not (role == "owner" or user.is_admin):
        raise Forbidden(f"only the owner of {subject} {name} (or a workspace owner) can hand it over")
    if to_owner == body["from_owner"]:
        raise InvalidInput(f"{to_owner} already owns {subject} {name}")
    if to_owner == user.id:
        raise InvalidInput("an ownership transfer is offered to someone else, who then accepts it")
    recipient = session.get(User, to_owner)
    if recipient is None or not recipient.active or not role_at_least(member_role(session, recipient, workspace_id), "editor"):
        raise InvalidInput(f"{to_owner} is not an active editor of this workspace and cannot own a definition")
    ws = get_workspace(session, workspace_id)
    apr = request_approval(session, workspace_id=workspace_id, run_id=None, action=ACTION, payload=body, plan_hash=None,
                           policy_version=ws.policy_version, requested_by=user.id, risk_tier="low",
                           destination=f"{subject}:{name}", affected_assets=[f"{subject}:{name}"],
                           evidence={"accept_by": to_owner, "reason": reason, "version": body["version"]})
    emit(workspace_id, "semantic.ownership_offered", {"approval_id": apr.id, "subject": subject, "name": name,
                                                      "from_owner": body["from_owner"], "to_owner": to_owner},
         actor=f"user:{user.id}", session=session)
    return apr


def _pending(session: Session, approval_id: str, workspace_id: str) -> Approval:
    apr = session.get(Approval, approval_id, with_for_update=True)
    if apr is None or apr.workspace_id != workspace_id or apr.action != ACTION:
        raise NotFound("ownership transfer not found")
    if apr.status != "pending":
        raise Conflict(f"the ownership transfer is {apr.status}")
    expires = apr.expires_at if apr.expires_at.tzinfo else apr.expires_at.replace(tzinfo=UTC)  # SQLite drops the zone
    if expires < utcnow():
        apr.status = "expired"
        raise Conflict("the ownership transfer offer expired")
    return apr


def accept(session: Session, user: User, workspace_id: str, approval_id: str) -> dict[str, Any]:
    """Only the named recipient accepts; the offer's hash must still match the definition as it is now."""
    from analystos.governance.approvals import claim

    apr = _pending(session, approval_id, workspace_id)
    body = dict(apr.payload or {})
    if body.get("to_owner") != user.id:
        audit(f"user:{user.id}", "semantic.ownership_accept_refused", workspace_id=workspace_id, target=apr.id,
              decision="deny", reasons=["not_the_recipient"], session=session)
        raise Forbidden("only the named new owner can accept this ownership transfer")
    require_role(session, user, workspace_id, "editor")
    now = payload(session, workspace_id, body["subject"], body["name"], user.id)
    ws = get_workspace(session, workspace_id)
    requester = session.get(User, apr.requested_by)
    why = None
    if stable_hash(now) != apr.payload_hash:
        why = "the definition or its owner changed since the offer"
    elif ws.policy_version != apr.policy_version:
        why = "the workspace policy changed since the offer"
    elif requester is None or not requester.active or \
            not role_at_least(member_role(session, requester, workspace_id), "editor"):
        why = "the person who offered it no longer holds edit rights"
    if why:  # the offer can never match again; it stays refused until it expires or is withdrawn
        raise PolicyDenied(f"ownership transfer refused: {why}; a new offer is required")
    claim(session, apr, expect="pending", to="executed", decided_by=user.id, decided_at=utcnow(), reason="accepted")
    changed = 0
    for r in _rows(session, workspace_id, body["subject"], body["name"]):
        if r.status != "rejected":
            r.owner_id = user.id
            changed += 1
    details = {"approval_id": apr.id, "subject": body["subject"], "name": body["name"], "from_owner": body["from_owner"],
               "to_owner": user.id, "version": body["version"], "payload_hash": apr.payload_hash, "rows": changed}
    emit(workspace_id, "semantic.ownership_transferred", details, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "semantic.ownership_transferred", workspace_id=workspace_id, target=apr.id, decision="allow",
          details=details, session=session)
    return details


def decline(session: Session, user: User, workspace_id: str, approval_id: str, *, reason: str | None = None) -> Approval:
    """The recipient declines, or the person who offered it withdraws."""
    from analystos.governance.approvals import claim

    apr = _pending(session, approval_id, workspace_id)
    if user.id not in ((apr.payload or {}).get("to_owner"), apr.requested_by):
        raise Forbidden("only the recipient or the person who offered it can decline an ownership transfer")
    claim(session, apr, expect="pending", to="rejected", decided_by=user.id, decided_at=utcnow(),
          reason=reason or ("withdrawn" if user.id == apr.requested_by else "declined"))
    emit(workspace_id, "semantic.ownership_declined", {"approval_id": apr.id, "by": user.id}, actor=f"user:{user.id}",
         session=session)
    return apr


__all__ = ["ACTION", "accept", "decline", "offer", "payload"]
