"""Capability registry API (P4-X01): list and inspect capabilities, per-workspace enablement, and
hot reload without a restart."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.capabilities import enablement, packs, registry
from analystos.contracts.capability import CapabilityManifest
from analystos.db.models import User
from analystos.governance.audit import audit
from analystos.governance.policy import require_role

router = APIRouter(prefix="/api", tags=["capabilities"])


class EnableIn(BaseModel):
    enabled: bool


def _snapshot(session: Session, workspace_id: str | None) -> registry.Snapshot:
    if workspace_id is None:
        return registry.current()
    try:
        from analystos.mcp.client import workspace_snapshot
    except ImportError:  # the MCP extra is optional
        return registry.current()
    return workspace_snapshot(session, workspace_id)


def _out(m: CapabilityManifest, enabled: bool | None) -> dict:
    return {"id": m.id, "kind": m.kind, "version": m.version, "ref": m.ref, "summary": m.summary, "source": m.source,
            "entry": m.entry, "determinism": m.determinism, "side_effect": m.side_effect, "cost_class": m.cost_class,
            "certification": m.certification.model_dump(), "autonomous_ok": m.autonomous_ok, "needs_approval": m.needs_approval,
            "tags": m.tags, "enabled": enabled}


@router.get("/capabilities")
def list_capabilities(kind: str | None = Query(None), workspace_id: str | None = Query(None),
                      user: User = Depends(current_user), session: Session = Depends(db)):
    """Every registered capability, filtered by kind; with `workspace_id`, its enablement there."""
    enabled: dict[str, bool] = {}
    if workspace_id is not None:
        require_role(session, user, workspace_id, "viewer")
    snap = _snapshot(session, workspace_id)
    if workspace_id is not None:
        enabled = enablement.enabled_map(session, workspace_id, snap)
    return {"digest": snap.digest, "capabilities": [_out(m, enabled.get(m.id) if workspace_id else None) for m in snap.list(kind)]}


@router.get("/capabilities/{capability_id}")
def get_capability(capability_id: str, _: User = Depends(current_user)):
    return registry.current().get(capability_id).model_dump()


@router.put("/workspaces/{workspace_id}/capabilities/{capability_id}")
def set_capability_enabled(workspace_id: str, capability_id: str, body: EnableIn, user: User = Depends(current_user),
                           session: Session = Depends(db)):
    return enablement.set_enabled(session, user, workspace_id, capability_id, body.enabled, _snapshot(session, workspace_id))


@router.post("/admin/capabilities/reload")
def reload_capabilities(admin: User = Depends(admin_user), session: Session = Depends(db)):
    """Rebuild the registry from every source and swap it in. A load that fails leaves the current
    registry in place (the error lists every problem). Runs in flight keep the versions they bound.
    Reloads this process; other processes (worker, scheduler) reload on their own call."""
    before = registry.current().digest
    snap = registry.reload()
    packs.reset()
    audit(f"user:{admin.id}", "capabilities.reloaded", details={"before": before, "after": snap.digest,
                                                               "count": len(snap.manifests)}, session=session)
    return {"digest": snap.digest, "previous_digest": before, "count": len(snap.manifests), "problems": list(snap.problems)}
