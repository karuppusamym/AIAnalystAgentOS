"""P4-S05 finding: the workspace list computed eight counts per workspace (8,000 statements at 1,000
workspaces). The grouped version returns the same counts with a statement count that does not grow
with the number of workspaces."""
from __future__ import annotations

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _statements(fn) -> int:
    n = {"count": 0}

    def listen(*_a, **_k):
        n["count"] += 1
    event.listen(Engine, "before_cursor_execute", listen)
    try:
        fn()
    finally:
        event.remove(Engine, "before_cursor_execute", listen)
    return n["count"]


def test_list_counts_match_detail_and_do_not_grow_with_workspaces(api):
    from analystos.artifacts.registry import save_artifact
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, User

    r = api.post("/api/auth/login", json={"email": "approver@analystos.local", "password": PASSWORD})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ids = [api.post("/api/workspaces", headers=h, json={"name": f"count {i}", "objective": "counting things"}).json()["id"]
           for i in range(3)]
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        for n, ws in enumerate(ids):
            for k in range(n):
                save_artifact(s, workspace_id=ws, run_id=None, type_="chart", name=f"c{k}", creator_agent="bi", content={"k": k})
                s.add(AnalysisRun(id=new_id("run"), workspace_id=ws, objective="counting things", status="COMPLETED", plan={},
                                  plan_version=1, scope={}, instructions=[], constraints={}, requested_by=user.id, summary={},
                                  origin={"type": "test"}))
            save_artifact(s, workspace_id=ws, run_id=None, type_="dataset", name="d", creator_agent="bi", content={})

    listed = {w["id"]: w["counts"] for w in api.get("/api/workspaces", headers=h).json()}
    for n, ws in enumerate(ids):
        assert listed[ws] == api.get(f"/api/workspaces/{ws}", headers=h).json()["counts"]
        assert listed[ws]["chart"] == n and listed[ws]["runs"] == n and listed[ws]["dataset"] == 1 and listed[ws]["sources"] == 0

    few = _statements(lambda: api.get("/api/workspaces", headers=h))
    ids += [api.post("/api/workspaces", headers=h, json={"name": f"more {i}", "objective": "counting things"}).json()["id"]
            for i in range(12)]
    many = _statements(lambda: api.get("/api/workspaces", headers=h))
    assert many == few, (few, many)
