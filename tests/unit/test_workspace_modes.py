"""Workspace presets must compose and keep the existing authority checks."""
from __future__ import annotations

import pytest

from analystos.capabilities import enablement, job_kinds, registry
from analystos.core.errors import Forbidden, InvalidInput
from analystos.db.models import User, WorkspaceMember
from analystos.services import workspace_modes
from analystos.services.workspaces import create_workspace, update_workspace


def _user(id: str) -> User:
    return User(id=id, email=f"{id}@example.org", name=id, password_hash="unused", is_admin=False,
                active=True, attributes={})


def test_modes_preview_is_read_only_and_apply_is_composable(sqlite_db):
    registry.reload(entry_points=False)
    with sqlite_db() as s:
        owner, viewer = _user("u_owner"), _user("u_viewer")
        s.add_all([owner, viewer])
        s.flush()
        ws = create_workspace(s, owner, name="Modes")
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=viewer.id, role="viewer"))
        s.flush()

        initial = enablement.overrides(s, ws.id)
        plan = workspace_modes.preview(s, owner, ws.id, ["ml", "analysis"])
        assert plan["selected"] == ["analysis", "ml"]
        assert initial == enablement.overrides(s, ws.id)
        assert workspace_modes.current(ws) == ["analysis", "engineering", "ml"]

        applied = workspace_modes.apply(s, owner, ws.id, ["ml", "analysis"])
        assert applied["current"] == ["analysis", "ml"]
        assert enablement.enabled_map(s, ws.id, registry.current())["playbook.prepare"] is False
        assert enablement.overrides(s, ws.id)["playbook.train"] is True
        assert enablement.enabled_map(s, ws.id, registry.current())["playbook.investigate"] is True
        assert workspace_modes.current(ws) == ["analysis", "ml"]

        with pytest.raises(Forbidden):
            workspace_modes.apply(s, viewer, ws.id, ["analysis"])
        with pytest.raises(InvalidInput):
            workspace_modes.apply(s, owner, ws.id, ["analysis", "analysis"])
        with pytest.raises(InvalidInput, match="work-modes endpoint"):
            update_workspace(s, owner, ws.id, {"settings": {"work_modes": ["ml"]}})
        assert workspace_modes.current(ws) == ["analysis", "ml"]


def test_new_analysis_workspace_can_enable_engineering_later(sqlite_db):
    registry.reload(entry_points=False)
    with sqlite_db() as s:
        owner = _user("u_owner")
        s.add(owner)
        s.flush()
        ws = create_workspace(s, owner, name="Engineering")
        workspace_modes.apply(s, owner, ws.id, ["analysis"])
        assert enablement.enabled_map(s, ws.id, registry.current())["playbook.train"] is False
        kinds = {k["key"]: k for k in job_kinds.availability(s, owner, ws.id)}
        assert kinds["prepare"]["mode"] == "engineering"
        assert "work_mode" in {r["code"] for r in kinds["prepare"]["reasons"]}
        assert "work_mode" not in {r["code"] for r in kinds["explain"]["reasons"]}
        workspace_modes.apply(s, owner, ws.id, ["analysis", "engineering"])
        explicit = enablement.overrides(s, ws.id)
        assert explicit["playbook.prepare"] is True
        assert enablement.enabled_map(s, ws.id, registry.current())["playbook.train"] is False
