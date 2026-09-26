"""Definition lifecycle API (ADR-0021, P7-03): drafts, publish, deprecate, retire, diff.

Every draft edit and state change takes `If-Match` (the ETag revision) and answers with the new
one; published versions are immutable. Built-in and pack playbooks are published by definition and
listed from the registry (`source: builtin`) next to the workspace's own versions.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.http import expected_revision, page, set_etag
from analystos.contracts.definition import DefinitionDraftIn, DefinitionPatch
from analystos.core.errors import InvalidInput
from analystos.db.models import Definition, User
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader
from analystos.services import definitions as svc

router = APIRouter(prefix="/api", tags=["definitions"])


class ReasonIn(BaseModel):
    reason: str | None = None


@scoped_loader
def _load(session: Session, user: User, workspace_id: str, definition_id: str, minimum: str = "viewer") -> Definition:
    return load_in_workspace(session, Definition, definition_id, workspace_id, user=user, minimum=minimum, label="definition",
                             for_update=minimum != "viewer")


@router.get("/workspaces/{workspace_id}/definitions")
def list_definitions(workspace_id: str, kind: str | None = None, key: str | None = None, status: str | None = None,
                     limit: int | None = None, cursor: str | None = None, include_builtin: bool = False,
                     user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Workspace definition versions, newest first, as `{items, next_cursor}` (plus `builtin` playbooks on request)."""
    require_role(session, user, workspace_id, "viewer")
    stmt = select(Definition).where(Definition.workspace_id == workspace_id)
    for column, value in ((Definition.kind, kind), (Definition.key, key), (Definition.status, status)):
        if value is not None:
            stmt = stmt.where(column == value)
    out = page(session, stmt, Definition, limit=limit, cursor=cursor,
               scope={"workspace": workspace_id, "list": "definitions", "kind": kind, "key": key, "status": status},
               render=lambda rs: [svc.out(r, spec=False) for r in rs])
    if include_builtin and kind in (None, "playbook"):
        from analystos.capabilities import registry

        out["builtin"] = [svc.builtin_ref(m).model_dump() for m in registry.current().list("Playbook")]
    return out


@router.post("/workspaces/{workspace_id}/definitions", status_code=201)
def create_draft(workspace_id: str, body: DefinitionDraftIn, response: Response, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function"),
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    from analystos.services.idempotency import Key, begin_in, complete_in

    require_role(session, user, workspace_id, "editor")
    key = Key.of(idempotency_key, principal=user.id, workspace_id=workspace_id, operation="definition.create",
                 request=body.model_dump())
    if key is not None and (replay := begin_in(session, key)) is not None:
        row = _load(session, user, workspace_id, replay.resource_id)
        response.headers["Idempotent-Replayed"] = "true"
    else:
        row = svc.create_draft(session, session.merge(user), workspace_id, body)
        if key is not None:
            complete_in(session, key, response={"id": row.id}, status_code=201, resource_type="definition", resource_id=row.id)
    set_etag(response, row.revision)
    return svc.out(row)


@router.get("/workspaces/{workspace_id}/definitions/{definition_id}")
def get_definition(workspace_id: str, definition_id: str, response: Response, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    row = _load(session, user, workspace_id, definition_id)
    set_etag(response, row.revision)
    return svc.out(row)


@router.patch("/workspaces/{workspace_id}/definitions/{definition_id}")
def patch_draft(workspace_id: str, definition_id: str, body: DefinitionPatch, response: Response,
                user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
                if_match: str | None = Header(default=None)):
    row = _load(session, user, workspace_id, definition_id, "editor")
    row = svc.update_draft(session, session.merge(user), row, body, expected_revision(if_match, required=True))
    set_etag(response, row.revision)
    return svc.out(row)


@router.post("/workspaces/{workspace_id}/definitions/{definition_id}/publish")
def publish(workspace_id: str, definition_id: str, response: Response, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function"), if_match: str | None = Header(default=None)):
    row = _load(session, user, workspace_id, definition_id, "editor")
    row = svc.publish(session, session.merge(user), row, expected_revision(if_match, required=True))
    set_etag(response, row.revision)
    return svc.out(row)


@router.post("/workspaces/{workspace_id}/definitions/{definition_id}/{action}")
def change_status(workspace_id: str, definition_id: str, action: str, body: ReasonIn, response: Response,
                  user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
                  if_match: str | None = Header(default=None)):
    """`deprecate` (keeps running with a warning) or `retire` (never runs again; pinned schedules block)."""
    if action not in ("deprecate", "retire"):
        raise InvalidInput("action must be deprecate or retire")
    row = _load(session, user, workspace_id, definition_id, "editor")
    expected = expected_revision(if_match, required=True)
    if expected is not None and expected != row.revision:
        from analystos.core.errors import PreconditionFailed

        raise PreconditionFailed(f"definition {row.id} is at revision {row.revision}, not {expected}",
                                 details={"current_revision": row.revision})
    fn = svc.deprecate if action == "deprecate" else svc.retire
    row = fn(session, session.merge(user), row, reason=body.reason)
    set_etag(response, row.revision)
    return svc.out(row)


@router.get("/workspaces/{workspace_id}/definitions/{definition_id}/diff")
def diff(workspace_id: str, definition_id: str, against: str | None = None, user: User = Depends(current_user),
         session: Session = Depends(db, scope="function")):
    """Field diff from this version to `against` (another version id), default: the newest published one."""
    row = _load(session, user, workspace_id, definition_id)
    other = _load(session, user, workspace_id, against) if against else \
        svc.latest_published(session, workspace_id, row.kind, row.key)
    if other is None:
        return {"from": svc.out(row, spec=False), "to": None, "changes": []}
    return {"from": svc.out(row, spec=False), "to": svc.out(other, spec=False), "changes": svc.diff(row.spec, other.spec)}
