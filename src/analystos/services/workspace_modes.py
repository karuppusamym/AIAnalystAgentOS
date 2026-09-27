"""Workspace work modes: a small, audited preset over existing playbook enablement."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.capabilities import enablement, registry
from analystos.core.errors import InvalidInput
from analystos.db.models import User, Workspace
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, require_role

MODES = ("analysis", "engineering", "ml")
ROOTS = {
    "analysis": ("playbook.investigate",),
    "engineering": ("playbook.prepare", "playbook.elt_build"),
    "ml": ("playbook.train", "playbook.score"),
}


def validate(modes: list[str]) -> list[str]:
    if not modes or any(not isinstance(m, str) or m not in MODES for m in modes) or len(modes) != len(set(modes)):
        raise InvalidInput("work modes must be a nonempty, unique selection of analysis, engineering and ml")
    return [m for m in MODES if m in modes]


def current(ws: Workspace) -> list[str]:
    raw = (ws.settings or {}).get("work_modes")
    # Older workspaces keep their existing range of choices until the owner edits it.
    if not isinstance(raw, list):
        return list(MODES)
    try:
        return validate(raw)
    except InvalidInput:
        return ["analysis"]


def preview(session: Session, user: User, workspace_id: str, modes: list[str]) -> dict[str, Any]:
    require_role(session, user, workspace_id, "owner")
    ws = get_workspace(session, workspace_id)
    selected = validate(modes)
    snap = registry.current()
    explicit = enablement.overrides(session, workspace_id)
    changes = []
    for mode, ids in ROOTS.items():
        for cid in ids:
            manifest = snap.manifests.get(cid)
            if manifest is None:
                # Optional packs do not make a preset invalid; the UI reports missing capabilities.
                changes.append({"capability_id": cid, "mode": mode, "enabled": mode in selected,
                                "available": False, "changed": False})
                continue
            desired = mode in selected
            effective = enablement.is_enabled(manifest, snap, explicit)
            changes.append({"capability_id": cid, "mode": mode, "enabled": desired,
                            "available": True, "changed": effective != desired})
    return {"workspace_id": workspace_id, "current": current(ws), "selected": selected, "capabilities": changes,
            "note": "Modes configure work shortcuts and playbook enablement. Source grants, roles and approvals remain separate."}


def apply(session: Session, user: User, workspace_id: str, modes: list[str]) -> dict[str, Any]:
    plan = preview(session, user, workspace_id, modes)
    ws = get_workspace(session, workspace_id)
    snap = registry.current()
    selected = plan["selected"]
    for change in plan["capabilities"]:
        if change["available"] and change["changed"]:
            enablement.set_enabled(session, user, workspace_id, change["capability_id"], change["enabled"], snap)
    ws.settings = {**(ws.settings or {}), "work_modes": selected}
    audit(f"user:{user.id}", "workspace.work_modes_set", workspace_id=workspace_id,
          details={"previous": plan["current"], "selected": selected}, session=session)
    return preview(session, user, workspace_id, selected)
