"""P4-09 single sign-on against a real HTTP identity provider on loopback (`scripts/fake_idp.py`: discovery, JWKS,
authorization with PKCE, token endpoint, RS256 ID tokens) — no transport injection, nothing external. The IdP's
groups map to workspace roles (config/oidc.yaml semantics), a group change at the IdP re-syncs the role at the
next sign-in, the admin SSO view names a mapping that grants nothing, the preview answers without signing anyone
in, and pilot readiness sees the mapping. Real SSO against a company IdP stays a pilot step. Skips cleanly without
the stack."""
from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
CLIENT_ID = "analystos-pilot"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def local_idp():
    import uvicorn

    spec = importlib.util.spec_from_file_location("fake_idp", ROOT / "scripts" / "fake_idp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    port = _free_port()
    issuer = f"http://127.0.0.1:{port}"
    app = mod.create_app(issuer, client_id=CLIENT_ID, users={
        "dana": {"sub": "idp-dana", "email": "dana@pilot.test", "name": "Dana", "email_verified": True,
                 "groups": ["pilot-viewers"], "department": "operations"}})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the local IdP did not start"
    yield issuer, app
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def sso(control_db, local_idp, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from analystos.api.app import app
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.security import oidc
    from analystos.services.workspaces import create_workspace

    issuer, idp_app = local_idp
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name=f"Pilot SSO {new_id('t')}", objective="sso rehearsal")
        s.flush()
        ws_id, ws_name = ws.id, ws.name
    mapping = tmp_path / "oidc.yaml"
    mapping.write_text(f"workspace_roles:\n  - {{group: pilot-viewers, workspace: '{ws_name}', role: viewer}}\n"
                       f"  - {{group: pilot-analysts, workspace: {ws_id}, role: analyst}}\n"
                       "  - {group: pilot-analysts, workspace: 'Workspace that was never created', role: editor}\n"
                       "attributes: {department: department}\n")
    for key, value in {"ANALYSTOS_OIDC_ISSUER": issuer, "ANALYSTOS_OIDC_CLIENT_ID": CLIENT_ID,
                       "ANALYSTOS_OIDC_REDIRECT_URI": "http://testserver/api/auth/oidc/callback",
                       "ANALYSTOS_OIDC_MAPPING_FILE": str(mapping), "ANALYSTOS_WEB_URL": "http://web.test"}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    oidc.set_http_transport(None)  # the real network: the IdP is an HTTP server on loopback
    with TestClient(app) as client:
        yield client, idp_app, ws_id
    get_settings.cache_clear()
    oidc.provider.cache_clear()


def _sign_in(client, idp_url_base: str, login: str) -> httpx.Response:
    start = client.get("/api/auth/oidc/login", params={"return_to": "/workspaces"}, follow_redirects=False)
    assert start.status_code == 302, start.text
    authorize = start.headers["location"]
    assert authorize.startswith(idp_url_base) and "code_challenge_method=S256" in authorize
    idp = httpx.get(authorize + f"&login_hint={login}", follow_redirects=False)  # the browser at the IdP
    assert idp.status_code == 302, idp.text
    q = {k: v[0] for k, v in parse_qs(urlsplit(idp.headers["location"]).query).items()}
    return client.get("/api/auth/oidc/callback", params={"code": q["code"], "state": q["state"]},
                      headers={"Accept": "application/json"})


def test_sso_against_a_local_http_idp_maps_and_resyncs_groups(sso, local_idp):
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.governance.policy import member_role

    client, idp_app, ws_id = sso
    issuer = local_idp[0]
    assert client.get("/api/auth/providers").json()["oidc"]["enabled"]
    r = _sign_in(client, issuer, "dana")
    assert r.status_code == 200, r.text
    token = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/auth/me", headers=token).json()["attributes"]["department"] == "operations"
    with session_scope() as s:
        dana = s.get(User, r.json()["user"]["id"])
        assert member_role(s, dana, ws_id) == "viewer"
    assert len(idp_app.state.issued) == 1 and idp_app.state.issued[0]["aud"] == CLIENT_ID

    # the IdP moves Dana into the analysts group: the next sign-in raises the role; the dead grant grants nothing
    idp_app.state.users["dana"]["groups"] = ["pilot-viewers", "pilot-analysts"]
    assert _sign_in(client, issuer, "dana").status_code == 200
    with session_scope() as s:
        assert member_role(s, s.get(User, dana.id), ws_id) == "analyst"
    # removed from every group: the SSO-granted membership is revoked at the next sign-in
    idp_app.state.users["dana"]["groups"] = []
    assert _sign_in(client, issuer, "dana").status_code == 200
    with session_scope() as s:
        assert member_role(s, s.get(User, dana.id), ws_id) is None

    # a user the IdP does not know never gets a code
    start = client.get("/api/auth/oidc/login", follow_redirects=False)
    denied = httpx.get(start.headers["location"] + "&login_hint=mallory", follow_redirects=False)
    assert "error=access_denied" in denied.headers["location"]


def test_admin_sso_view_preview_and_readiness(sso):
    client, _, ws_id = sso
    admin = client.post("/api/auth/login", json={"email": "admin@analystos.local", "password": "ChangeMe123!"}).json()
    h = {"Authorization": f"Bearer {admin['access_token']}"}
    view = client.get("/api/admin/sso", headers=h).json()
    assert view["enabled"] and view["client_id"] == CLIENT_ID and "secret" not in str(view).lower()
    assert view["problems"] == ["group pilot-analysts: workspace 'Workspace that was never created' does not exist; "
                                "the grant (editor) has no effect"]
    preview = client.post("/api/admin/sso/preview", headers=h, json={"groups": ["pilot-viewers", "pilot-analysts"]}).json()
    assert preview["signs_in"] and [(r["workspace_id"], r["role"]) for r in preview["roles"]] == [(ws_id, "analyst")]
    assert preview["unresolved"] == ["Workspace that was never created"]
    analyst = client.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "ChangeMe123!"}).json()
    assert client.get("/api/admin/sso", headers={"Authorization": f"Bearer {analyst['access_token']}"}).status_code == 403
    checks = client.get(f"/api/workspaces/{ws_id}/pilot-readiness", headers=h).json()["checks"]
    sso_check = next(c for c in checks if c["check"] == "identity.sso")
    assert sso_check["status"] == "pass" and "pilot-analysts -> analyst" in sso_check["reason"]
