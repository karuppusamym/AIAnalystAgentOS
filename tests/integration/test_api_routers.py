"""P4-C10: every API router through FastAPI's TestClient, with a positive test and negative
authorization tests (no token -> 401, wrong role -> 403, non-member -> 404, which hides whether the
workspace exists)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str, password: str = PASSWORD) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(api):
    """analyst owns workspace W; approver is a viewer of W; outsider is a platform user outside W."""
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Artifact, RunEvent, User

    admin = _login(api, "admin@analystos.local")
    analyst = _login(api, "analyst@analystos.local")
    viewer = _login(api, "approver@analystos.local")
    outsider_email = f"outsider-{new_id('x')}@analystos.local"
    r = api.post("/api/users", headers=admin, json={"email": outsider_email, "name": "Outsider", "password": PASSWORD})
    assert r.status_code == 200, r.text
    outsider = _login(api, outsider_email)
    r = api.post("/api/workspaces", headers=analyst, json={"name": "router tests", "objective": "Find the drivers of SLA breaches"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "viewer"}).status_code == 200
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        run = AnalysisRun(id=new_id("run"), workspace_id=ws, objective="Find the drivers of SLA breaches", status="COMPLETED",
                          plan={"steps": []}, plan_version=1, scope={}, instructions=[], constraints={}, requested_by=owner.id,
                          summary={}, origin={"type": "user"})
        s.add(run)
        s.flush()
        s.add(RunEvent(workspace_id=ws, run_id=run.id, type="run.status", payload={"status": "COMPLETED"}))
        metric = Artifact(id=new_id("art"), workspace_id=ws, run_id=run.id, type="metric", name="record_count", plan_version=1,
                          status="validated", content={"name": "record_count", "sql_expression": "COUNT(*)"}, content_hash="h")
        s.add(metric)
        run_id, metric_id = run.id, metric.id
    return {"ws": ws, "run": run_id, "metric": metric_id, "admin": admin, "analyst": analyst, "viewer": viewer,
            "outsider": outsider}


# -------------------------------------------------------------------------------------------- auth
def test_auth_login_me_and_user_admin(api, world):
    me = api.get("/api/auth/me", headers=world["analyst"])
    assert me.status_code == 200 and me.json()["email"] == "analyst@analystos.local" and "password_hash" not in me.json()
    assert api.get("/api/users", headers=world["analyst"]).status_code == 200


def test_auth_negative(api, world):
    assert api.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": "wrong"}).status_code == 401
    assert api.get("/api/auth/me").status_code == 401
    assert api.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    r = api.post("/api/users", headers=world["analyst"], json={"email": "x@y.z", "name": "x", "password": "p"})
    assert r.status_code == 403  # only platform administrators create users


# ------------------------------------------------------------------------------------------- admin
def test_admin_positive(api, world):
    assert api.get("/api/admin/settings", headers=world["admin"]).status_code == 200
    assert api.get("/api/agents", headers=world["analyst"]).status_code == 200
    assert api.get(f"/api/workspaces/{world['ws']}/context", headers=world["viewer"]).status_code == 200


def test_admin_negative(api, world):
    assert api.get("/api/admin/settings").status_code == 401
    assert api.get("/api/admin/settings", headers=world["analyst"]).status_code == 403
    assert api.get("/api/admin/audit", headers=world["analyst"]).status_code == 403
    assert api.get(f"/api/workspaces/{world['ws']}/context", headers=world["outsider"]).status_code == 404
    r = api.post(f"/api/workspaces/{world['ws']}/context", headers=world["viewer"], json={"kind": "note", "name": "n", "body": "b"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------------------- analysis
def test_analysis_positive(api, world):
    ws, run = world["ws"], world["run"]
    listed = api.get(f"/api/workspaces/{ws}/analysis", headers=world["viewer"])
    assert listed.status_code == 200 and [r["id"] for r in listed.json()] == [run]
    detail = api.get(f"/api/workspaces/{ws}/analysis/{run}", headers=world["analyst"])
    assert detail.status_code == 200 and detail.json()["status"] == "COMPLETED"
    with api.stream("GET", f"/api/workspaces/{ws}/analysis/{run}/events", headers=world["viewer"]) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: run.status" in body and "event: end" in body


def test_analysis_negative(api, world):
    ws, run = world["ws"], world["run"]
    assert api.get(f"/api/workspaces/{ws}/analysis").status_code == 401
    assert api.get(f"/api/workspaces/{ws}/analysis", headers=world["outsider"]).status_code == 404
    assert api.get(f"/api/workspaces/{ws}/analysis/{run}", headers=world["outsider"]).status_code in (403, 404)
    assert api.get(f"/api/workspaces/{ws}/analysis/{run}/events").status_code == 401
    assert api.get(f"/api/workspaces/{ws}/analysis/{run}/events", headers=world["outsider"]).status_code in (403, 404)
    r = api.post(f"/api/workspaces/{ws}/analysis", headers=world["viewer"], json={"objective": "Find the drivers of SLA breaches"})
    assert r.status_code == 403  # viewers cannot start runs
    assert api.post(f"/api/workspaces/{ws}/analysis/{run}/cancel", headers=world["viewer"]).status_code == 403


# --------------------------------------------------------------------------------------- artifacts
def test_artifacts_positive(api, world):
    arts = api.get(f"/api/workspaces/{world['ws']}/artifacts", headers=world["viewer"])
    assert arts.status_code == 200 and world["metric"] in {a["id"] for a in arts.json()}
    one = api.get(f"/api/artifacts/{world['metric']}", headers=world["analyst"])
    assert one.status_code == 200 and one.json()["name"] == "record_count"
    assert api.get(f"/api/workspaces/{world['ws']}/approvals", headers=world["viewer"]).status_code == 200


def test_artifacts_negative(api, world):
    assert api.get(f"/api/artifacts/{world['metric']}").status_code == 401
    assert api.get(f"/api/artifacts/{world['metric']}", headers=world["outsider"]).status_code == 404
    assert api.get(f"/api/workspaces/{world['ws']}/insights", headers=world["outsider"]).status_code == 404
    assert api.post(f"/api/artifacts/{world['metric']}/publish", headers=world["viewer"]).status_code == 403


# ----------------------------------------------------------------------------------------- catalog
def test_catalog_positive(api, world):
    kinds = api.get("/api/source-kinds", headers=world["analyst"])
    assert kinds.status_code == 200 and any(k["kind"] == "postgres" for k in kinds.json())
    assert api.get(f"/api/workspaces/{world['ws']}/catalog", headers=world["viewer"]).json() == []
    assert api.get(f"/api/workspaces/{world['ws']}/crawls", headers=world["viewer"]).status_code == 200


def test_catalog_negative(api, world):
    assert api.get("/api/source-kinds").status_code == 401
    assert api.get(f"/api/workspaces/{world['ws']}/catalog", headers=world["outsider"]).status_code == 404
    assert api.get(f"/api/workspaces/{world['ws']}/crawls", headers=world["outsider"]).status_code == 404


# -------------------------------------------------------------------------------------- continuous
def test_continuous_positive(api, world):
    body = {"name": "Volume above 1", "kind": "metric_threshold", "config": {"metric": "record_count", "grain": "month", "op": ">", "value": 1}}
    created = api.post(f"/api/workspaces/{world['ws']}/monitors", headers=world["analyst"], json=body)
    assert created.status_code == 200, created.text
    again = api.post(f"/api/workspaces/{world['ws']}/monitors", headers=world["analyst"], json=body)
    assert again.json()["id"] == created.json()["id"]  # P4-C08: one monitor per condition
    listed = api.get(f"/api/workspaces/{world['ws']}/monitors", headers=world["viewer"])
    assert [m["id"] for m in listed.json()] == [created.json()["id"]]
    assert api.get(f"/api/workspaces/{world['ws']}/alerts", headers=world["viewer"]).status_code == 200
    assert api.get("/api/notifications", headers=world["viewer"]).status_code == 200


def test_continuous_negative(api, world):
    body = {"name": "m", "kind": "metric_threshold", "config": {"metric": "record_count", "op": ">", "value": 2}}
    assert api.post(f"/api/workspaces/{world['ws']}/monitors", json=body).status_code == 401
    assert api.post(f"/api/workspaces/{world['ws']}/monitors", headers=world["viewer"], json=body).status_code == 403
    assert api.post(f"/api/workspaces/{world['ws']}/monitors", headers=world["outsider"], json=body).status_code == 404
    assert api.get(f"/api/workspaces/{world['ws']}/alerts", headers=world["outsider"]).status_code == 404
    schedule = {"name": "weekly", "kind": "reanalysis", "cron": "0 7 * * 1", "config": {}}
    assert api.post(f"/api/workspaces/{world['ws']}/schedules", headers=world["viewer"], json=schedule).status_code == 403
    assert api.get("/api/notifications").status_code == 401


# -------------------------------------------------------------------------------------- workspaces
def test_workspaces_positive(api, world):
    mine = api.get("/api/workspaces", headers=world["analyst"])
    assert mine.status_code == 200 and world["ws"] in {w["id"] for w in mine.json()}
    one = api.get(f"/api/workspaces/{world['ws']}", headers=world["viewer"])
    assert one.status_code == 200 and one.json()["role"] == "viewer"
    assert api.get(f"/api/workspaces/{world['ws']}/sources", headers=world["viewer"]).status_code == 200
    r = api.patch(f"/api/workspaces/{world['ws']}", headers=world["analyst"], json={"description": "router test"})
    assert r.status_code == 200


def test_workspaces_negative(api, world):
    assert api.get("/api/workspaces").status_code == 401
    assert world["ws"] not in {w["id"] for w in api.get("/api/workspaces", headers=world["outsider"]).json()}
    assert api.get(f"/api/workspaces/{world['ws']}", headers=world["outsider"]).status_code == 404
    assert api.patch(f"/api/workspaces/{world['ws']}", headers=world["viewer"], json={"description": "x"}).status_code == 403
    assert api.delete(f"/api/workspaces/{world['ws']}", headers=world["viewer"]).status_code == 403
    r = api.post(f"/api/workspaces/{world['ws']}/members", headers=world["viewer"], json={"email": "admin@analystos.local", "role": "owner"})
    assert r.status_code == 403
