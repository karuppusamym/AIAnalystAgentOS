"""Typed work orders API (workbench API §2, P4-06): create (idempotent), list (cursor pages), read
(ETag), edit (If-Match required) and start (idempotent, If-Match)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.http import expected_revision, page, set_etag
from analystos.api.serialize import row
from analystos.contracts.work import WorkOrderSpec
from analystos.db.models import User, WorkOrder
from analystos.governance.policy import load_in_workspace, require_role
from analystos.services import work_orders as svc

router = APIRouter(prefix="/api", tags=["work-orders"])


@router.post("/workspaces/{workspace_id}/work-orders", status_code=201)
def create_work_order(workspace_id: str, body: WorkOrderSpec, response: Response, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function"),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    from analystos.services.idempotency import Key, begin_in, complete_in

    require_role(session, user, workspace_id, "analyst")
    key = Key.of(idempotency_key, principal=user.id, workspace_id=workspace_id, operation="work_order.create",
                 request=body.model_dump(mode="json"))
    if key is not None and (replay := begin_in(session, key)) is not None:
        wo = load_in_workspace(session, WorkOrder, replay.resource_id, workspace_id, user=user, label="work order")
        response.headers["Idempotent-Replayed"] = "true"
    else:
        wo = svc.create(session, session.merge(user), workspace_id, body)
        if key is not None:
            complete_in(session, key, response={"id": wo.id}, status_code=201, resource_type="work_order", resource_id=wo.id)
    set_etag(response, wo.revision)
    return svc.out(wo)


@router.get("/workspaces/{workspace_id}/work-orders")
def list_work_orders(workspace_id: str, limit: int | None = None, cursor: str | None = None, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(WorkOrder).where(WorkOrder.workspace_id == workspace_id)
    return page(session, stmt, WorkOrder, limit=limit, cursor=cursor, scope={"workspace": workspace_id, "list": "work_orders"},
                render=lambda rs: [svc.out(w) for w in rs])


@router.get("/workspaces/{workspace_id}/work-orders/{work_order_id}")
def get_work_order(workspace_id: str, work_order_id: str, response: Response, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    wo = load_in_workspace(session, WorkOrder, work_order_id, workspace_id, user=user, label="work order")
    set_etag(response, wo.revision)
    return svc.out(wo)


@router.patch("/workspaces/{workspace_id}/work-orders/{work_order_id}")
def patch_work_order(workspace_id: str, work_order_id: str, body: WorkOrderSpec, response: Response,
                     user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
                     if_match: str | None = Header(default=None)):
    """Replace the typed spec: a new revision. `If-Match` is required (428 without, 412 when stale)."""
    expected = expected_revision(if_match, required=True)
    wo = load_in_workspace(session, WorkOrder, work_order_id, workspace_id, user=user, minimum="analyst", label="work order",
                           for_update=True)
    wo = svc.update(session, session.merge(user), wo, body, expected)
    set_etag(response, wo.revision)
    return svc.out(wo)


@router.post("/workspaces/{workspace_id}/work-orders/{work_order_id}/runs", status_code=202)
def start_work_order(workspace_id: str, work_order_id: str, response: Response, user: User = Depends(current_user),
                     if_match: str | None = Header(default=None),
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Start the work order's revision named by `If-Match` (required). Returns the run (202: it executes
    in the durable workers). A pipeline or ML work order is refused with `unsupported_capability`."""
    from analystos.services.idempotency import Key

    expected = expected_revision(if_match, required=True)
    key = Key.of(idempotency_key, principal=user.id, workspace_id=workspace_id, operation="work_order.start",
                 request={"work_order_id": work_order_id, "revision": expected})
    run, replayed = svc.start(user, workspace_id, work_order_id, expected_revision=expected, idempotency=key)
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    response.headers["Location"] = f"/api/workspaces/{workspace_id}/analysis/{run.id}"
    return row(run)
