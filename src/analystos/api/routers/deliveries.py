"""External delivery API (N-3): destinations authorized by hash-bound approvals, and their deliveries.

Authorization is decided in the approvals inbox (`POST /api/approvals/{id}/approve`); nothing here sends.
Sends happen in the scheduler loop, which re-verifies the approval immediately before each one.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.db.models import Approval, Delivery, DeliveryDestination, User
from analystos.governance.policy import load_in_workspace, require_role
from analystos.services import deliveries as svc

router = APIRouter(prefix="/api", tags=["deliveries"])


class DeliveryDestinationIn(BaseModel):
    name: str
    kind: str
    config: dict = {}
    content_kinds: list[str] | None = None


class DeliveryDestinationPatch(BaseModel):
    name: str | None = None
    config: dict | None = None
    content_kinds: list[str] | None = None


class DeliveryRevokeIn(BaseModel):
    reason: str | None = None


class DeliveryIn(BaseModel):
    destination_id: str
    subject_type: str
    subject_id: str


def destination_out(session: Session, dest: DeliveryDestination) -> dict:
    approval = session.get(Approval, dest.approval_id) if dest.approval_id else None
    # The secret reference is an operator detail: say whether there is one, not which (as the MCP router does).
    config = {k: v for k, v in (dest.config or {}).items() if k != "secret_ref"}
    return {**row(dest, exclude={"config"}), "config": config, "has_secret": bool((dest.config or {}).get("secret_ref")),
            "authorized": svc._authorized(dest), "summary": svc.describe(dest),
            "approval": {"id": approval.id, "status": approval.status} if approval else None}


@router.get("/workspaces/{workspace_id}/delivery-destinations")
def list_destinations(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    found = session.scalars(select(DeliveryDestination).where(DeliveryDestination.workspace_id == workspace_id)
                            .order_by(DeliveryDestination.created_at))
    return [destination_out(session, d) for d in found]


@router.post("/workspaces/{workspace_id}/delivery-destinations")
def create_destination(workspace_id: str, body: DeliveryDestinationIn, user: User = Depends(current_user),
                       session: Session = Depends(db, scope="function")):
    """Registers the destination and requests its authorization; it sends nothing until an approver decides."""
    dest = svc.create_destination(session, session.merge(user), workspace_id, **body.model_dump())
    session.flush()
    return destination_out(session, dest)


@router.patch("/delivery-destinations/{destination_id}")
def patch_destination(destination_id: str, body: DeliveryDestinationPatch, response: Response, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function"), if_match: str | None = Header(default=None)):
    """A changed target (recipients, URL, secret reference, content kinds) needs a new authorization."""
    from analystos.api.http import expected_revision, set_etag

    load_in_workspace(session, DeliveryDestination, destination_id, user=user, minimum="editor", label="delivery destination")
    dest = svc.update_destination(session, session.merge(user), destination_id, **body.model_dump(),
                                  expected_revision=expected_revision(if_match, required=False))
    session.flush()
    set_etag(response, dest.revision)
    return destination_out(session, dest)


@router.post("/delivery-destinations/{destination_id}/revoke")
def revoke_destination(destination_id: str, body: DeliveryRevokeIn | None = None, user: User = Depends(current_user),
                       session: Session = Depends(db, scope="function")):
    load_in_workspace(session, DeliveryDestination, destination_id, user=user, minimum="editor", label="delivery destination")
    dest = svc.revoke_destination(session, session.merge(user), destination_id, body.reason if body else None)
    session.flush()
    return destination_out(session, dest)


@router.post("/delivery-destinations/{destination_id}/reauthorize")
def reauthorize_destination(destination_id: str, user: User = Depends(current_user),
                            session: Session = Depends(db, scope="function")):
    """Request a new authorization for the same target after the previous one lapsed or was rejected."""
    load_in_workspace(session, DeliveryDestination, destination_id, user=user, minimum="editor", label="delivery destination")
    dest = svc.reauthorize_destination(session, session.merge(user), destination_id)
    session.flush()
    return destination_out(session, dest)


@router.get("/workspaces/{workspace_id}/deliveries")
def list_deliveries(workspace_id: str, status: str | None = None, destination_id: str | None = None,
                    user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(Delivery).where(Delivery.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Delivery.status == status)
    if destination_id:
        stmt = stmt.where(Delivery.destination_id == destination_id)
    return rows(session.scalars(stmt.order_by(Delivery.created_at.desc()).limit(200)))


@router.post("/workspaces/{workspace_id}/deliveries")
def queue_delivery(workspace_id: str, body: DeliveryIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    """Send a final report or an alert to one authorized destination (queued; idempotent per content)."""
    require_role(session, user, workspace_id, "editor")
    found = svc.enqueue(session, workspace_id, destination_ids=[body.destination_id], subject_type=body.subject_type,
                        subject_id=body.subject_id, origin={"user": user.id})
    if not found:
        from analystos.core.errors import NotFound

        raise NotFound("delivery destination not found")
    session.flush()
    return row(found[0])


@router.post("/deliveries/{delivery_id}/redrive")
def redrive_delivery(delivery_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    load_in_workspace(session, Delivery, delivery_id, user=user, minimum="editor", label="delivery")
    d = svc.redrive(session, session.merge(user), delivery_id)
    session.flush()
    return row(d)
