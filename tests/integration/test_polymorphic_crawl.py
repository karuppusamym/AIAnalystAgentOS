"""A reference column whose values live in several tables' keys is recorded by the crawl as a polymorphic reference on
the column (and reaches the export and the suggested model's issues), never queued as a relationship."""
from __future__ import annotations

import pandas as pd
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


@pytest.fixture(scope="module")
def env(api):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.sources import discover_source, register_source

    r = api.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": PASSWORD})
    analyst = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = api.post("/api/workspaces", headers=analyst, json={"name": "polymorphic", "objective": "Tickets"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    folder = get_settings().upload_dir / ws / "tickets"
    folder.mkdir(parents=True, exist_ok=True)
    for name, prefix, n in (("incident", "inc", 60), ("sc_task", "tsk", 30), ("change_request", "chg", 10)):
        pd.DataFrame({"sys_id": [f"{prefix}{i}" for i in range(1, n + 1)]}).to_parquet(folder / f"{name}.parquet", index=False)
    refs = [f"inc{1 + i % 60}" for i in range(100)] + [f"tsk{1 + i % 30}" for i in range(60)] + [f"chg{1 + i % 10}" for i in range(40)]
    pd.DataFrame({"activity_id": [f"a{i}" for i in range(200)], "task_sys_id": refs,
                  "activity": ["Created"] * 200}).to_parquet(folder / "task_activity.parquet", index=False)
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, user, ws, kind="csv", name="tickets", config={"path": f"{ws}/tickets"}, secret_ref=None)
        s.flush()
        src_id = src.id
        s.expunge(user)
    discover_source(user, src_id)
    r = api.put(f"/api/workspaces/{ws}/sources/{src_id}/selection", headers=analyst,
                json={"assets": ["incident", "sc_task", "change_request", "task_activity"]})
    assert r.status_code == 200, r.text
    return {"ws": ws, "analyst": analyst, "crawl": r.json()["profile_crawl"]["crawl_id"]}


def test_the_crawl_records_the_tables_a_reference_resolves_to_and_queues_no_relationship(api, env):
    from analystos.db.base import session_scope
    from analystos.db.models import CrawlRun, SemanticRelationshipCandidate

    with session_scope() as s:
        assert s.get(CrawlRun, env["crawl"]).status == "succeeded"
        assert not [c for c in s.scalars(select(SemanticRelationshipCandidate).where(
            SemanticRelationshipCandidate.workspace_id == env["ws"])) if "task_sys_id" in c.from_columns]
    cat = {a["name"]: a for a in api.get(f"/api/workspaces/{env['ws']}/catalog", headers=env["analyst"]).json()}
    col = next(c for c in cat["task_activity"]["columns"] if c["name"] == "task_sys_id")
    poly = col.get("polymorphic_reference")
    assert poly and poly["coverage"] == 1.0
    assert [t["asset"].split(".")[-1] for t in poly["targets"]] == ["incident", "sc_task", "change_request"]


def test_the_export_and_the_suggestion_say_so(api, env):
    base = f"/api/workspaces/{env['ws']}"
    doc = api.get(f"{base}/context/export?format=json", headers=env["analyst"]).json()
    asset = next(a for a in doc["assets"] if a["name"] == "task_activity")
    col = next(c for c in asset["columns"] if c["name"] == "task_sys_id")
    assert col["polymorphic_reference"]["coverage"] == 1.0
    suggestion = api.get(f"{base}/semantic/model/suggestion", headers=env["analyst"]).json()
    assert any(i["code"] == "polymorphic_reference" for i in suggestion["issues"])
