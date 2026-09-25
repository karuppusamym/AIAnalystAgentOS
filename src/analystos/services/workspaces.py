from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.models import User, Workspace, WorkspaceMember
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, load_policy, require_role, save_policy
from analystos.security.auth import ROLE_RANK


def create_workspace(session: Session, user: User, *, name: str, description: str = "", objective: str = "",
                     autonomy_level: int = 3, policy: dict | None = None) -> Workspace:
    if not 0 <= autonomy_level <= 4:
        raise InvalidInput("autonomy_level must be 0..4")
    ws = Workspace(id=new_id("ws"), name=name, description=description, objective=objective, autonomy_level=autonomy_level,
                   created_by=user.id, settings={}, policy_version=0)
    session.add(ws)
    session.flush()
    session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
    save_policy(session, ws, WorkspacePolicyDoc.model_validate(policy or {}), user.id)
    emit(ws.id, "workspace.created", {"name": name}, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "workspace.created", workspace_id=ws.id, session=session)
    return ws


def list_workspaces(session: Session, user: User) -> list[Workspace]:
    stmt = select(Workspace).where(Workspace.deleted_at.is_(None))
    if not user.is_admin:
        stmt = stmt.join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id).where(WorkspaceMember.user_id == user.id)
    return list(session.scalars(stmt.order_by(Workspace.created_at.desc())))


def update_workspace(session: Session, user: User, workspace_id: str, patch: dict) -> Workspace:
    require_role(session, user, workspace_id, "editor")
    ws = get_workspace(session, workspace_id)
    for key in ("name", "description", "objective"):
        if key in patch and patch[key] is not None:
            setattr(ws, key, patch[key])
    if patch.get("autonomy_level") is not None:
        if not 0 <= int(patch["autonomy_level"]) <= 4:
            raise InvalidInput("autonomy_level must be 0..4")
        require_role(session, user, workspace_id, "owner")
        ws.autonomy_level = int(patch["autonomy_level"])
    if patch.get("settings") is not None:
        ws.settings = {**(ws.settings or {}), **patch["settings"]}
    emit(ws.id, "workspace.updated", {k: v for k, v in patch.items() if v is not None}, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "workspace.updated", workspace_id=ws.id, details=patch, session=session)
    return ws


def delete_workspace(session: Session, user: User, workspace_id: str) -> None:
    require_role(session, user, workspace_id, "owner")
    ws = get_workspace(session, workspace_id)
    ws.deleted_at = utcnow()
    audit(f"user:{user.id}", "workspace.deleted", workspace_id=ws.id, session=session)


def set_policy(session: Session, user: User, workspace_id: str, doc: dict) -> int:
    require_role(session, user, workspace_id, "owner")
    ws = get_workspace(session, workspace_id)
    merged = {**load_policy(session, ws).model_dump(), **doc}
    version = save_policy(session, ws, WorkspacePolicyDoc.model_validate(merged), user.id)
    audit(f"user:{user.id}", "policy.updated", workspace_id=ws.id, details={"version": version}, session=session)
    return version


def add_member(session: Session, user: User, workspace_id: str, email: str, role: str) -> WorkspaceMember:
    require_role(session, user, workspace_id, "owner")
    if role not in ROLE_RANK:
        raise InvalidInput(f"role must be one of {sorted(ROLE_RANK)}")
    target = session.scalar(select(User).where(User.email == email.lower()))
    if target is None:
        raise NotFound(f"user {email} not found")
    member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id,
                                                          WorkspaceMember.user_id == target.id))
    if member is None:
        member = WorkspaceMember(workspace_id=workspace_id, user_id=target.id, role=role)
        session.add(member)
    else:
        member.role = role
    audit(f"user:{user.id}", "member.set", workspace_id=workspace_id, target=target.id, details={"role": role}, session=session)
    return member


def remove_member(session: Session, user: User, workspace_id: str, user_id: str) -> None:
    require_role(session, user, workspace_id, "owner")
    member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id,
                                                          WorkspaceMember.user_id == user_id))
    if member:
        session.delete(member)
        audit(f"user:{user.id}", "member.removed", workspace_id=workspace_id, target=user_id, session=session)
