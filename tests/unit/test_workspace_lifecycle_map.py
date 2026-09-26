"""Workspace suspension and the registry's resolved agent bindings."""
from __future__ import annotations

import pytest

from analystos.api.routers.capabilities import _agent_bindings
from analystos.capabilities import registry
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id
from analystos.db.models import AnalysisRun, User, WorkspaceMember
from analystos.governance.policy import get_workspace, require_role
from analystos.runtime.engine import get_state
from analystos.services.workspaces import create_workspace, list_workspaces, update_workspace


def test_workspace_disable_blocks_access_and_owner_can_reactivate(sqlite_db):
    with sqlite_db() as session:
        owner = User(id="u_owner", email="owner@example.org", name="Owner", password_hash="unused",
                     is_admin=False, active=True, attributes={})
        viewer = User(id="u_viewer", email="viewer@example.org", name="Viewer", password_hash="unused",
                      is_admin=False, active=True, attributes={})
        session.add_all([owner, viewer])
        session.flush()
        ws = create_workspace(session, owner, name="Lifecycle test")
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=viewer.id, role="viewer"))
        session.flush()

        assert update_workspace(session, owner, ws.id, {"status": "disabled"}).status == "disabled"
        run_id = new_id("run")
        session.add(AnalysisRun(id=run_id, workspace_id=ws.id, objective="Lifecycle test", status="NEW",
                                plan={}, plan_version=0, scope={}, instructions=[], constraints={},
                                requested_by=owner.id, summary={}, origin={"type": "test"}))
        session.commit()
        assert get_state(run_id) == {"control": "cancel"}
        assert ws in list_workspaces(session, owner)
        with pytest.raises(NotFound):
            get_workspace(session, ws.id)
        with pytest.raises(NotFound):
            require_role(session, viewer, ws.id, "viewer")
        with pytest.raises(InvalidInput, match="reactivate"):
            update_workspace(session, owner, ws.id, {"status": "disabled", "name": "renamed"})
        assert update_workspace(session, owner, ws.id, {"status": "active"}).status == "active"
        assert require_role(session, viewer, ws.id, "viewer") == "viewer"


def test_agent_map_resolves_manifest_capabilities_and_tools():
    snap = registry.load(packs_dir=None, entry_points=False)
    agent = snap.get("agent.metadata")
    mapping = _agent_bindings(agent, snap, {"skill.catalog_lookup": False, "tool.metadata_read": True})
    ids = {c["id"] for c in mapping["capabilities"]}
    assert set(agent.spec["capabilities"]).issubset(ids)
    assert all(c["kind"] in ("Skill", "Tool", "Method") for c in mapping["capabilities"])
    assert mapping["tools"] == sorted(agent.spec["tools"])
    assert mapping["playbooks"]
