"""OIDC SSO end to end through the API (P4-S04, SEC-001..003): login redirect with PKCE, the fake
IdP's authorization and token endpoints, callback, user provisioning, group -> role sync on the next
login, and ABAC attributes from the ID token narrowing `resolve_scope`."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select
from tests.oidc_fake import CLIENT_ID, ISSUER, FakeIdP

pytestmark = pytest.mark.integration


@pytest.fixture
def sso(control_db, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.security import oidc
    from analystos.services.workspaces import create_workspace

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name=f"SSO ITSM {new_id('t')}", objective="sso test")
        s.flush()
        ws_id, ws_name = ws.id, ws.name
    mapping = tmp_path / "oidc.yaml"
    mapping.write_text(
        "platform_admin_groups: [aos-admins]\n"
        f"workspace_roles:\n  - {{group: itsm-viewers, workspace: '{ws_name}', role: viewer}}\n"
        f"  - {{group: itsm-analysts, workspace: {ws_id}, role: analyst}}\n"
        "attributes: {department: department, pii_clearance: pii_clearance}\n")
    idp = FakeIdP()
    for key, value in {"ANALYSTOS_OIDC_ISSUER": ISSUER, "ANALYSTOS_OIDC_CLIENT_ID": CLIENT_ID,
                       "ANALYSTOS_OIDC_REDIRECT_URI": "http://testserver/api/auth/oidc/callback",
                       "ANALYSTOS_OIDC_MAPPING_FILE": str(mapping), "ANALYSTOS_WEB_URL": "http://web.test"}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    oidc.set_http_transport(idp.transport())
    with TestClient(app) as client:
        yield client, idp, ws_id
    oidc.set_http_transport(None)
    get_settings.cache_clear()


def _sign_in(client, idp, claims, *, accept_json=True):
    start = client.get("/api/auth/oidc/login", params={"return_to": "/workspaces"}, follow_redirects=False)
    assert start.status_code == 302 and "aos_oidc_tx" in start.headers["set-cookie"]
    assert "HttpOnly" in start.headers["set-cookie"]
    code, state = idp.authorize(start.headers["location"], claims)
    headers = {"Accept": "application/json"} if accept_json else {}
    return client.get("/api/auth/oidc/callback", params={"code": code, "state": state}, headers=headers, follow_redirects=False)


def test_providers_endpoint_reports_sso(sso):
    client, _, _ = sso
    body = client.get("/api/auth/providers").json()
    assert body["password"] is True and body["oidc"]["enabled"] and body["oidc"]["login_url"] == "/api/auth/oidc/login"


def test_sso_login_provisions_the_user_and_syncs_roles(sso):
    from analystos.db.base import session_scope
    from analystos.db.models import AuditEvent, User, UserIdentity, WorkspaceMember
    from analystos.governance.policy import member_role

    client, idp, ws_id = sso
    claims = {"sub": "idp-user-1", "email": "Dana@Corp.test", "name": "Dana", "groups": ["itsm-viewers", "itsm-analysts"],
              "department": "ops", "email_verified": True}
    r = _sign_in(client, idp, claims)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == "dana@corp.test" and body["return_to"] == "/workspaces"
    token = {"Authorization": f"Bearer {body['access_token']}"}
    me = client.get("/api/auth/me", headers=token).json()
    assert me["attributes"]["department"] == "ops" and me["attributes"]["groups"] == ["itsm-analysts", "itsm-viewers"]
    assert not me["is_admin"]
    assert client.get(f"/api/workspaces/{ws_id}", headers=token).status_code == 200
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "dana@corp.test"))
        assert member_role(s, user, ws_id) == "analyst"  # highest of the mapped groups
        assert s.scalar(select(UserIdentity).where(UserIdentity.subject == "idp-user-1")).managed_memberships == {ws_id: "analyst"}
        # no password can ever open an SSO-created account
        assert client.post("/api/auth/login", json={"email": "dana@corp.test", "password": "!sso"}).status_code == 401

    # next login: the analyst group is gone -> the SSO grant drops to viewer; an admin group -> platform admin
    r = _sign_in(client, idp, {**claims, "groups": ["itsm-viewers", "aos-admins"], "department": "finance"}, accept_json=False)
    assert r.status_code == 302 and r.headers["location"].startswith("http://web.test/login#sso_token=")
    token = {"Authorization": f"Bearer {parse_qs(urlsplit(r.headers['location']).fragment)['sso_token'][0]}"}
    me = client.get("/api/auth/me", headers=token).json()
    assert me["is_admin"] and me["attributes"]["department"] == "finance"
    with session_scope() as s:
        role = s.scalar(select(WorkspaceMember.role).where(WorkspaceMember.workspace_id == ws_id,
                                                           WorkspaceMember.user_id == me["id"]))
        assert role == "viewer"

    # last login: no mapped group at all -> the SSO-granted membership is revoked, admin removed
    _sign_in(client, idp, {**claims, "groups": []})
    with session_scope() as s:
        assert s.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id,
                                                      WorkspaceMember.user_id == me["id"])) is None
        assert s.get(User, me["id"]).is_admin is False
        actions = {a for (a,) in s.execute(select(AuditEvent.action).where(AuditEvent.actor == f"user:{me['id']}"))}
        assert {"auth.sso_user_created", "auth.sso_login"} <= actions


def test_callback_rejects_a_forged_state_and_an_unverified_email_link(sso):
    client, idp, _ = sso
    start = client.get("/api/auth/oidc/login", follow_redirects=False)
    code, _state = idp.authorize(start.headers["location"], {"sub": "x", "email": "x@corp.test"})
    r = client.get("/api/auth/oidc/callback", params={"code": code, "state": "forged"},
                   headers={"Accept": "application/json"})
    assert r.status_code == 401 and "state" in r.json()["error"]["message"]
    # an existing local account is only linked when the IdP verified the email
    r = _sign_in(client, idp, {"sub": "attacker", "email": "analyst@analystos.local", "email_verified": False})
    assert r.status_code == 401 and "verify" in r.json()["error"]["message"]


def test_abac_attributes_from_the_idp_narrow_the_scope(sso, analytics_plane):
    from analystos.contracts.policy import AttributeRule
    from analystos.db.base import session_scope
    from analystos.db.models import Source, SourceAsset, SourceColumn, User, Workspace
    from analystos.governance.policy import load_policy, resolve_scope, save_policy

    client, idp, ws_id = sso
    token = _sign_in(client, idp, {"sub": "idp-ops", "email": "ops@corp.test", "groups": ["itsm-analysts"],
                                   "department": "ops"}).json()
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        src = Source(id=f"src_{ws_id[-8:]}", workspace_id=ws_id, kind="postgres", name="hr", config={}, status="ready")
        s.add(src)
        s.flush()
        for name, cols in (("payroll", ["employee", "salary"]), ("incident", ["priority", "caller"])):
            asset = SourceAsset(id=f"ast_{name}_{ws_id[-6:]}", source_id=src.id, workspace_id=ws_id, schema_name="hr", name=name,
                                source_name=name, selected=True)
            s.add(asset)
            s.flush()
            for i, c in enumerate(cols):
                s.add(SourceColumn(asset_id=asset.id, name=c, data_type="text", ordinal=i))
        policy = load_policy(s, ws)
        policy.attribute_rules = [AttributeRule(id="payroll-hr", assets=["hr.payroll"], require={"department": ["hr"]})]
        save_policy(s, ws, policy, token["user"]["id"])
    with session_scope() as s:
        user = s.get(User, token["user"]["id"])
        scope = resolve_scope(s, user, ws_id)
        assert "hr.incident" in scope.assets and "hr.payroll" not in scope.assets
    _sign_in(client, idp, {"sub": "idp-ops", "email": "ops@corp.test", "groups": ["itsm-analysts"], "department": "hr"})
    with session_scope() as s:
        scope = resolve_scope(s, s.get(User, token["user"]["id"]), ws_id)
        assert "hr.payroll" in scope.assets
