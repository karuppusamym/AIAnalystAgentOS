"""Data shape on the real stack: a crawled workspace (a star, an event log, a table with odd names) gets its patterns and
next steps, a person's confirm/dismiss survives a re-crawl, the model's proposal for the odd table is verified before it is
shown, viewers can neither read nor decide (the data scope needs an analyst)."""
from __future__ import annotations

from types import SimpleNamespace

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


def _login(api, email):
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def env(api):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.sources import discover_source, register_source

    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    r = api.post("/api/workspaces", headers=analyst, json={"name": "data shape", "objective": "Understand the data"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "viewer"}).status_code == 200
    folder = get_settings().upload_dir / ws / "shop"
    folder.mkdir(parents=True, exist_ok=True)
    n = 300
    pd.DataFrame({"customer_id": list(range(1, 11)), "segment": ["Enterprise", "SMB"] * 5,
                  "city": [f"City {i % 4}" for i in range(10)]}).to_parquet(folder / "customers.parquet", index=False)
    pd.DataFrame({"order_id": list(range(1, n + 1)), "customer_id": [1 + i % 10 for i in range(n)],
                  "amount": [10.0 + i for i in range(n)], "qty": [1 + i % 7 for i in range(n)],
                  "channel": ["web", "shop", "phone", "app"] * (n // 4), "late": [i % 4 == 0 for i in range(n)],
                  "ordered_at": pd.date_range("2026-01-01", periods=n, freq="D")}).to_parquet(folder / "orders.parquet", index=False)
    steps = ["Created", "Assigned", "Worked", "Closed"]
    pd.DataFrame({"event_id": list(range(400)), "task_ref": [f"T{i // 4}" for i in range(400)],
                  "activity": [steps[i % 4] for i in range(400)],
                  "activity_at": pd.date_range("2026-01-01", periods=400, freq="h")}).to_parquet(folder / "task_activity.parquet", index=False)
    pd.DataFrame({"k1": [f"C{i // 5}" for i in range(300)], "s9": [["a", "b", "c", "d", "e"][i % 5] for i in range(300)],
                  "t3": pd.date_range("2026-02-01", periods=300, freq="h")}).to_parquet(folder / "misc.parquet", index=False)
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, user, ws, kind="csv", name="shop", config={"path": f"{ws}/shop"}, secret_ref=None)
        s.flush()
        src_id = src.id
        s.expunge(user)
    discover_source(user, src_id)
    r = api.put(f"/api/workspaces/{ws}/sources/{src_id}/selection", headers=analyst,
                json={"assets": ["customers", "orders", "task_activity", "misc"]})
    assert r.status_code == 200, r.text
    return {"ws": ws, "src": src_id, "analyst": analyst, "viewer": approver, "base": f"/api/workspaces/{ws}/data-shape"}


def _tables(api, env, who="analyst"):
    r = api.get(env["base"], headers=env[who])
    assert r.status_code == 200, r.text
    return r.json(), {t["name"]: t for t in r.json()["tables"]}


def _kinds(t):
    return {p["kind"]: p for p in t["patterns"]}


def test_a_crawled_workspace_shows_its_patterns_and_next_steps(api, env):
    doc, tables = _tables(api, env)
    assert doc["version"] == "data-shape/1" and "star" in [w["kind"] for w in doc["workspace"]]
    orders = _kinds(tables["orders"])
    assert {"time_series", "ml_candidate"} <= set(orders)
    assert ("late", "classification") in {(t["column"], t["kind"]) for t in orders["ml_candidate"]["detail"]["targets"]}
    assert orders["time_series"]["next_step"]["action"] == "investigation"
    assert orders["ml_candidate"]["next_step"]["action"] == "experiment"
    log = _kinds(tables["task_activity"])["event_log"]
    assert log["detail"]["mapping"]["case_column"] == "task_ref" and log["next_step"]["action"] == "process_analysis"
    assert log["state"] == "suggested" and log["origin"] == "rules" and log["reasons"]
    assert "event_log" not in _kinds(tables["orders"]) and "event_log" not in _kinds(tables["customers"])
    assert doc["summary"]["event_log"] == 1


def test_a_person_confirms_or_dismisses_and_a_recrawl_keeps_the_answer(api, env):
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset
    from analystos.services.crawler import KEPT_SEMANTICS

    assert "shape" in KEPT_SEMANTICS
    _, tables = _tables(api, env)
    orders, log = tables["orders"]["asset_id"], tables["task_activity"]["asset_id"]
    r = api.post(f"{env['base']}/mark", headers=env["analyst"], json={"asset_id": log, "kind": "event_log", "decision": "confirm"})
    assert r.status_code == 200 and r.json()["confirmed"] == ["event_log"]
    r = api.post(f"{env['base']}/mark", headers=env["analyst"], json={"asset_id": orders, "kind": "ml_candidate", "decision": "dismiss"})
    assert r.status_code == 200 and r.json()["dismissed"] == ["ml_candidate"]
    _, tables = _tables(api, env)
    assert _kinds(tables["task_activity"])["event_log"]["state"] == "confirmed"
    assert "ml_candidate" not in _kinds(tables["orders"]) and "time_series" in _kinds(tables["orders"])
    with session_scope() as s:  # a re-derivation of the table's semantics keeps the person's answer
        row = s.get(SourceAsset, orders)
        kept = {k: v for k, v in (row.semantics or {}).items() if k in KEPT_SEMANTICS}
        assert kept["shape"]["dismissed"] == ["ml_candidate"]
    api.post(f"{env['base']}/mark", headers=env["analyst"], json={"asset_id": orders, "kind": "ml_candidate", "decision": "reset"})
    assert "ml_candidate" in _kinds(_tables(api, env)[1]["orders"])


def test_viewers_cannot_read_or_decide_and_bad_input_is_refused(api, env):
    assert api.get(env["base"], headers=env["viewer"]).status_code == 403  # the caller's data scope needs an analyst
    _, tables = _tables(api, env)
    body = {"asset_id": tables["orders"]["asset_id"], "kind": "ml_candidate", "decision": "dismiss"}
    assert api.post(f"{env['base']}/mark", headers=env["viewer"], json=body).status_code == 403
    assert api.post(f"{env['base']}/propose", headers=env["viewer"]).status_code == 403
    assert api.post(f"{env['base']}/mark", headers=env["analyst"], json={**body, "kind": "olap"}).status_code == 422
    assert api.post(f"{env['base']}/mark", headers=env["analyst"], json={**body, "asset_id": "ast_nope"}).status_code == 404


class FakeRouter:
    def __init__(self, data, mode="auto"):
        self.data, self._mode, self.calls, self.skips = data, mode, [], []

    def mode(self, purpose):
        return self._mode

    def available(self, purpose, ctx=None):
        return True

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append(purpose)

    def complete_json(self, purpose, system, user, *, ctx=None, max_tokens=0):
        self.calls.append((purpose, user))
        return SimpleNamespace(model="m-small", data=self.data)


def test_a_model_proposal_is_verified_in_code_before_it_is_shown(api, env, monkeypatch):
    from analystos.runtime import context

    good = {"table": "misc", "pattern": "event_log", "case_column": "k1", "activity_column": "s9", "timestamp_column": "t3",
            "resource_column": None, "reason": "k1 repeats, s9 has five values, t3 is a time"}
    bad = {"table": "customers", "pattern": "event_log", "case_column": "customer_id", "activity_column": "segment",
           "timestamp_column": "city"}
    router = FakeRouter({"tables": [good, bad, {"table": "ghost", "pattern": "event_log"}]})
    monkeypatch.setattr(context, "default_router", lambda: router)
    r = api.post(f"{env['base']}/propose", headers=env["analyst"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["called"] and out["proposed"] == 1 and router.calls[0][0] == "data_shape_proposal"
    assert '"k1"' in router.calls[0][1] and "Enterprise" not in router.calls[0][1]  # names, types and counts; no values
    misc = _kinds(_tables(api, env)[1]["misc"])["event_log"]
    assert misc["state"] == "proposed" and misc["origin"] == "model" and misc["detail"]["mapping"]["activity_column"] == "s9"
    off = FakeRouter({}, mode="off")
    monkeypatch.setattr(context, "default_router", lambda: off)
    assert api.post(f"{env['base']}/propose", headers=env["analyst"]).json()["called"] is False
