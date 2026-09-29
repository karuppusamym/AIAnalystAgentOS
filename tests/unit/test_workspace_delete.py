"""Deleting a workspace archives it (audited, nothing purged), including one that was disabled first."""
from __future__ import annotations

import pytest

from analystos.core.errors import Forbidden, NotFound


@pytest.fixture
def world(sqlite_db):
    from analystos.db.models import User, Workspace, WorkspaceMember

    with sqlite_db() as s:
        s.add(User(id="owner", email="owner@x", name="owner", password_hash="x", active=True))
        s.add(User(id="analyst", email="analyst@x", name="analyst", password_hash="x", active=True))
        s.add(Workspace(id="ws_on", name="on", created_by="owner"))
        s.add(Workspace(id="ws_off", name="off", created_by="owner", status="disabled"))
        for ws in ("ws_on", "ws_off"):
            s.add(WorkspaceMember(workspace_id=ws, user_id="owner", role="owner"))
            s.add(WorkspaceMember(workspace_id=ws, user_id="analyst", role="analyst"))
        s.commit()
    return sqlite_db


@pytest.mark.parametrize("ws_id", ["ws_on", "ws_off"])
def test_owner_archives_an_active_or_disabled_workspace(world, ws_id):
    from analystos.db.models import User, Workspace
    from analystos.services.workspaces import delete_workspace, list_workspaces

    with world() as s:
        delete_workspace(s, s.get(User, "owner"), ws_id)
        s.commit()
    with world() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.deleted_at is not None and ws.status == "disabled"
        assert ws_id not in {w.id for w in list_workspaces(s, s.get(User, "owner"))}


def test_only_an_owner_deletes_and_an_archived_workspace_stays_gone(world):
    from analystos.db.models import User
    from analystos.services.workspaces import delete_workspace

    with world() as s, pytest.raises(Forbidden):
        delete_workspace(s, s.get(User, "analyst"), "ws_off")
    with world() as s:
        delete_workspace(s, s.get(User, "owner"), "ws_off")
        s.commit()
    with world() as s, pytest.raises(NotFound):
        delete_workspace(s, s.get(User, "owner"), "ws_off")
