"""OIDC group -> workspace role provisioning (SEC-001..003, P4-09) on the SQLite control plane: the highest role
any group grants wins, however the mapping names the workspace (id or name); a group removed at the IdP revokes
exactly the membership it granted; a manual membership is never touched. No services."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from analystos.db import models
from analystos.db.models import User, WorkspaceMember
from analystos.security import oidc
from analystos.services.workspaces import create_workspace

ISSUER = "https://idp.pilot.test"


@pytest.fixture
def world(sqlite_db):
    models.Base.metadata.tables["user_identity"].create(sqlite_db.kw["bind"])
    s = sqlite_db()
    owner = User(id="usr_owner", email="owner@x.test", name="owner", password_hash="!", is_admin=False, attributes={})
    s.add(owner)
    s.flush()
    ws = create_workspace(s, owner, name="Pilot ITSM", objective="pilot")
    other = create_workspace(s, owner, name="Pilot finance", objective="pilot")
    s.commit()
    yield s, ws, other
    s.close()


def _role(s, user_id: str, ws_id: str) -> str | None:
    return s.scalar(select(WorkspaceMember.role).where(WorkspaceMember.user_id == user_id, WorkspaceMember.workspace_id == ws_id))


def test_a_workspace_mapped_by_id_and_by_name_gets_the_highest_role(world):
    """Before the fix the last-resolved key won: `leads` (editor, by id) then `viewers` (viewer, by name) made a viewer."""
    s, ws, _ = world
    mapping = oidc.Mapping(workspace_roles=[oidc.RoleGrant(group="leads", workspace=ws.id, role="editor"),
                                            oidc.RoleGrant(group="viewers", workspace=ws.name, role="viewer")])
    user = oidc.provision(s, {"sub": "s-1", "email": "lee@pilot.test", "groups": ["leads", "viewers"]}, issuer=ISSUER,
                          mapping=mapping)
    assert _role(s, user.id, ws.id) == "editor"
    reverse = oidc.Mapping(workspace_roles=list(reversed(mapping.workspace_roles)))
    user2 = oidc.provision(s, {"sub": "s-2", "email": "kim@pilot.test", "groups": ["leads", "viewers"]}, issuer=ISSUER,
                           mapping=reverse)
    assert _role(s, user2.id, ws.id) == "editor"  # the order of the mapping file does not matter


def test_group_changes_resync_only_the_memberships_sso_granted(world):
    s, ws, other = world
    mapping = oidc.Mapping(workspace_roles=[oidc.RoleGrant(group="itsm-analysts", workspace=ws.name, role="analyst"),
                                            oidc.RoleGrant(group="itsm-leads", workspace=ws.id, role="editor")])
    claims = {"sub": "s-3", "email": "ana@pilot.test", "groups": ["itsm-analysts"]}
    user = oidc.provision(s, claims, issuer=ISSUER, mapping=mapping)
    s.add(WorkspaceMember(workspace_id=other.id, user_id=user.id, role="viewer"))  # granted by hand, not by SSO
    s.flush()
    oidc.provision(s, {**claims, "groups": ["itsm-analysts", "itsm-leads"]}, issuer=ISSUER, mapping=mapping)
    assert _role(s, user.id, ws.id) == "editor"
    oidc.provision(s, {**claims, "groups": []}, issuer=ISSUER, mapping=mapping)  # removed from every group at the IdP
    assert _role(s, user.id, ws.id) is None and _role(s, user.id, other.id) == "viewer"
