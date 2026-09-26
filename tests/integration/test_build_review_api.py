"""P4-U05 backend on the compose Postgres: the build review endpoints the Build UI reads (job list with
approval state, job detail with its approval, the per-file diff against the previous job) and the KPI
editor's validation. No dbt needed: jobs are recorded rows, as the elt_plan step would leave them."""
from __future__ import annotations

from datetime import timedelta

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


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(api):
    from analystos.core.ids import utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import Approval, BuildJob

    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    ws = api.post("/api/workspaces", headers=analyst, json={"name": "build review", "objective": "Build KPIs"}).json()["id"]
    other = api.post("/api/workspaces", headers=approver, json={"name": "someone else's", "objective": "x"}).json()["id"]
    now = utcnow()

    def job(i: str, workspace: str, target: str, files: dict, status: str, age: int, approval: str | None = None) -> BuildJob:
        return BuildJob(id=i, workspace_id=workspace, run_id=f"run_{i}", source_run_id="run_src", engine="postgres:analytics",
                        target_schema=target, project_name="aos", project_files=files, project_hash=f"h_{i}", relations=[],
                        dry_run={"ok": True}, estimate={"rows": 10}, rollback={}, status=status, created_by="agent:builder",
                        approval_id=approval, created_at=now - timedelta(minutes=age))

    v1 = {"dbt_project.yml": "name: aos\n", "models/incident.sql": "select * from src\n", "models/old.yml": "x: 1\n"}
    v2 = {"dbt_project.yml": "name: aos\n", "models/incident.sql": "select * from src\nwhere active\n", "models/metrics.yml": "m: 1\n"}
    with session_scope() as s:
        s.add(Approval(id="apr_bld2", workspace_id=ws, run_id="run_bld2", action="elt_build", payload={"job_id": "bld_2"},
                       payload_hash="p" * 64, plan_hash="q" * 64, policy_version=1, requested_by="usr_x", status="pending",
                       risk_tier="high", destination="postgres:analytics/aos_mart", affected_assets=[], evidence={},
                       expires_at=now + timedelta(hours=24)))
        s.add_all([job("bld_0", ws, "other_mart", {"a.sql": "select 0\n"}, "succeeded", 30),
                   job("bld_1", ws, "aos_mart", v1, "succeeded", 20),
                   job("bld_2", ws, "aos_mart", v2, "awaiting_approval", 10, "apr_bld2"),
                   job("bld_x", other, "aos_mart", {"secret.sql": "select secret\n"}, "succeeded", 40)])
    return {"ws": ws, "other": other, "analyst": analyst, "approver": approver}


def test_list_carries_approval_state_and_detail_the_approval(api, world):
    jobs = api.get(f"/api/workspaces/{world['ws']}/builds", headers=world["analyst"]).json()
    assert [j["id"] for j in jobs] == ["bld_2", "bld_1", "bld_0"]
    assert jobs[0]["approval_status"] == "pending" and jobs[1]["approval_status"] is None
    assert "project_files" not in jobs[0]
    detail = api.get("/api/builds/bld_2", headers=world["analyst"]).json()
    assert detail["approval"]["id"] == "apr_bld2" and detail["approval"]["status"] == "pending"
    assert detail["approval"]["payload_hash"] == "p" * 64 and "payload" not in detail["approval"]


def test_diff_against_the_previous_job_for_the_same_target(api, world):
    d = api.get("/api/builds/bld_2/diff", headers=world["analyst"])
    assert d.status_code == 200, d.text
    d = d.json()
    assert d["basis"] == "previous_job_same_target" and d["against"]["job_id"] == "bld_1" and not d["identical"]
    assert {f["path"]: f["status"] for f in d["files"]} == {"dbt_project.yml": "unchanged", "models/incident.sql": "modified",
                                                            "models/metrics.yml": "added", "models/old.yml": "removed"}
    assert "+where active" in next(f["diff"] for f in d["files"] if f["path"] == "models/incident.sql")
    first = api.get("/api/builds/bld_1/diff", headers=world["analyst"]).json()
    assert first["basis"] == "none" and first["against"] is None and {f["status"] for f in first["files"]} == {"added"}
    explicit = api.get("/api/builds/bld_2/diff", params={"against": "bld_0"}, headers=world["analyst"]).json()
    assert explicit["basis"] == "requested" and explicit["summary"]["removed"] == 1


def test_diff_never_reads_another_workspace(api, world):
    refused = api.get("/api/builds/bld_2/diff", params={"against": "bld_x"}, headers=world["analyst"])
    assert refused.status_code == 404 and "secret" not in refused.text
    assert api.get("/api/builds/bld_x/diff", headers=world["analyst"]).status_code in (403, 404)
    assert api.get("/api/builds/bld_x", headers=world["analyst"]).status_code in (403, 404)


def test_kpi_validation_reports_problems_and_conflicts_without_recording(api, world):
    from analystos.db.base import session_scope
    from analystos.db.models import SemanticMetric

    base = f"/api/workspaces/{world['ws']}/semantic/metrics"
    bad = api.post(f"{base}/validate", headers=world["analyst"], json={"name": "p1 count", "expression": "priority"}).json()
    assert not bad["ok"] and bad["problems"][0]["field"] == "name"
    shape = api.post(f"{base}/validate", headers=world["analyst"], json={"name": "p1_count", "expression": "priority"}).json()
    assert shape["problems"] == [{"field": "expression", "message": "not an aggregate expression"}]
    # a malformed name is a 422 with per-field problems, not a server error
    r = api.post(base, headers=world["analyst"], json={"name": "p1 count", "expression": "COUNT(*)"})
    assert r.status_code == 422 and r.json()["error"]["details"]["problems"][0]["field"] == "name"

    assert api.post(base, headers=world["analyst"], json={"name": "p1_count", "expression": "COUNT(*)"}).status_code == 200
    same = api.post(f"{base}/validate", headers=world["analyst"], json={"name": "p1_count", "expression": "count(*)"}).json()
    assert same["ok"] and same["existing"] == {"version": 1, "status": "proposed"} and same["conflicts"] == []
    dup = api.post(f"{base}/validate", headers=world["analyst"], json={"name": "incidents", "expression": "COUNT(*)"}).json()
    assert [c["kind"] for c in dup["conflicts"]] == ["duplicate_expression"] and "p1_count" in dup["conflicts"][0]["names"]
    competing = api.post(f"{base}/validate", headers=world["analyst"], json={"name": "p1_count", "expression": "SUM(x)"}).json()
    assert [c["kind"] for c in competing["conflicts"]] == ["conflicting_definition"]
    with session_scope() as s:  # validation recorded nothing
        assert {m.name for m in s.scalars(select(SemanticMetric).where(SemanticMetric.workspace_id == world["ws"]))} == {"p1_count"}
    assert api.post(f"/api/workspaces/{world['other']}/semantic/metrics/validate", headers=world["analyst"],
                    json={"name": "a", "expression": "COUNT(*)"}).status_code in (403, 404)
