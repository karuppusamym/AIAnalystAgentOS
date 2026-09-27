"""Stream A on the real stack: selecting tables profiles them in the background (the first discovery crawl could
not: the source was not ready), the crawl measures declared and discovered joins and queues them for review, the
data-model suggestion reads it all, validation measures keys and fan-out through the gateway, the proposal goes
through structure review, and a person's acceptance of a join flips readiness (the relationship table is updated)."""
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


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def env(api):
    """A fact (orders), a dimension (customers) and a composite-key table (order_lines) in one staged source."""
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.sources import discover_source, register_source

    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    r = api.post("/api/workspaces", headers=analyst, json={"name": "model suggestion", "objective": "Model the shop"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "approver"}).status_code == 200
    folder = get_settings().upload_dir / ws / "shop"
    folder.mkdir(parents=True, exist_ok=True)
    n = 60
    pd.DataFrame({"customer_id": list(range(1, 11)), "segment": ["Enterprise", "SMB"] * 5,
                  "customer_name": [f"Customer {i}" for i in range(1, 11)]}).to_parquet(folder / "customers.parquet", index=False)
    pd.DataFrame({"order_id": list(range(1, n + 1)), "customer_id": [1 + i % 10 for i in range(n)],
                  "amount": [10.0 + i for i in range(n)],
                  "ordered_at": pd.date_range("2026-01-01", periods=n, freq="D")}).to_parquet(folder / "orders.parquet", index=False)
    pd.DataFrame({"order_id": [1 + i // 3 for i in range(3 * n)], "line_no": [1 + i % 3 for i in range(3 * n)],
                  "qty": [1 + i % 4 for i in range(3 * n)]}).to_parquet(folder / "order_lines.parquet", index=False)
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, user, ws, kind="csv", name="shop", config={"path": f"{ws}/shop"}, secret_ref=None)
        s.flush()
        src_id = src.id
        s.expunge(user)
    discover_source(user, src_id)
    return {"ws": ws, "src": src_id, "analyst": analyst, "approver": approver, "base": f"/api/workspaces/{ws}/semantic"}


def _assets(ws: str) -> dict:
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset

    with session_scope() as s:
        rows = list(s.scalars(select(SourceAsset).where(SourceAsset.workspace_id == ws)))
        s.expunge_all()
    return {a.name: a for a in rows}


def test_selection_profiles_in_the_background_and_measures_joins(api, env):
    from analystos.db.base import session_scope
    from analystos.db.models import CrawlRun, SemanticRelationshipCandidate

    assert not any((a.stats or {}).get("profile_meta") for a in _assets(env["ws"]).values())  # discovery: not ready
    r = api.put(f"/api/workspaces/{env['ws']}/sources/{env['src']}/selection", headers=env["analyst"],
                json={"assets": ["customers", "orders", "order_lines"]})
    assert r.status_code == 200, r.text
    pc = r.json()["profile_crawl"]
    assert pc["status"] == "scheduled" and pc["via"] == "background" and len(pc["assets"]) == 3
    with session_scope() as s:  # TestClient runs background tasks before returning
        run = s.get(CrawlRun, pc["crawl_id"])
        assert run.status == "succeeded", run.error
        assert run.trigger == "selection" and run.stats["profiled"] == 3 and run.stats["relationship_candidates"] >= 1
    assets = _assets(env["ws"])
    meta = assets["orders"].stats["profile_meta"]
    assert meta["fingerprint"] == assets["orders"].fingerprint and meta["source"] == "snapshot" and meta["rows_profiled"] == 60
    with session_scope() as s:
        cands = {(c.from_asset.split(".")[-1], tuple(c.from_columns), c.to_asset.split(".")[-1]): c
                 for c in s.scalars(select(SemanticRelationshipCandidate).where(
                     SemanticRelationshipCandidate.workspace_id == env["ws"]))}
    c = cands[("orders", ("customer_id",), "customers")]
    assert c.status == "pending" and c.origin == "crawler" and c.cardinality == "many_to_one"
    assert c.assessment["approvable"]
    # a column profile's facts reach the rule description; a small enumeration is listed
    cat = {a["name"]: a for a in api.get(f"/api/workspaces/{env['ws']}/catalog", headers=env["analyst"]).json()}
    seg = next(col for col in cat["customers"]["columns"] if col["name"] == "segment")
    assert seg["profile"]["values_complete"] and sorted(seg["profile"]["values"]) == ["Enterprise", "SMB"]
    assert (seg["description_origin"], seg["description"]) == ("rule", "Descriptive attribute; always present; one of Enterprise, SMB.")
    amount = next(col for col in cat["orders"]["columns"] if col["name"] == "amount")
    assert amount["description"] == "Monetary amount; always present; from 10 to 69."
    assert cat["orders"]["time_column"] == "ordered_at" and cat["orders"]["profile_meta"]["rows_profiled"] == 60


def test_model_suggestion_validate_and_propose(api, env):
    base, analyst = env["base"], env["analyst"]
    doc = api.get(f"{base}/model/suggestion", headers=analyst)
    assert doc.status_code == 200, doc.text
    doc = doc.json()
    tables = {t["name"]: t for t in doc["tables"]}
    assert set(tables) == {"customers", "orders", "order_lines"}
    assert tables["orders"]["time_column"] == "ordered_at" and "amount" in tables["orders"]["measures"]
    assert tables["customers"]["primary_key"]["columns"] == ["customer_id"]
    assert tables["customers"]["primary_key"]["evidence"] in ("profile_unique", "measured_unique")
    rel = next(r for r in doc["relationships"] if r["from"]["fq"].endswith(".orders") and r["to"]["fq"].endswith(".customers"))
    assert rel["status"] == "pending" and rel["candidate_id"] and rel["cardinality"] == "many_to_one"
    assert doc["summary"]["tables"] == 3 and doc["summary"]["relationships_pending"] >= 1
    assert any(m["expression"] == "SUM(amount)" for m in doc["metrics"])
    val = api.post(f"{base}/model/suggestion/validate", headers=analyst)
    assert val.status_code == 200, val.text
    val = val.json()
    by = {t["asset_id"]: t for t in val["tables"]}
    assert by[tables["customers"]["asset_id"]] == {"asset_id": tables["customers"]["asset_id"], "rows": 10,
                                                   "distinct_keys": 10, "unique": True}
    join = next(j for j in val["joins"] if j["from"].endswith(".orders") and j["to"].endswith(".customers"))
    assert (join["rows_before"], join["rows_after"], join["fans_out"]) == (60, 60, False)
    assert 0 < val["queries"] <= 40
    again = api.get(f"{base}/model/suggestion", headers=analyst).json()
    assert again["summary"]["keys_measured"] == 3
    # order_lines had no known key: validation searched and measured its composite key
    lines = next(t for t in again["tables"] if t["name"] == "order_lines")["primary_key"]
    assert set(lines["columns"]) == {"order_id", "line_no"} and lines["evidence"] == "measured_unique" and lines["unique"]
    assert tables["order_lines"]["primary_key"]["evidence"] == "none"
    prop = api.post(f"{base}/model/suggestion/propose", headers=analyst)
    assert prop.status_code == 200, prop.text
    assert prop.json()["status"] == "proposed" and prop.json()["model_version"]
    # the approval stays with another person
    own = api.post(f"{base}/model/approve", headers=analyst, json={"version": prop.json()["model_version"]})
    assert own.status_code == 403


def test_accepting_the_join_flips_readiness(api, env):
    base, analyst, approver = env["base"], env["analyst"], env["approver"]
    assets = _assets(env["ws"])
    pair = [f"{assets['orders'].schema_name}.orders", f"{assets['customers'].schema_name}.customers"]

    def fanout() -> dict:
        r = api.post(f"/api/workspaces/{env['ws']}/readiness", headers=analyst, json={"job_kind": "compare", "assets": pair})
        assert r.status_code == 201, r.text
        return next(c for c in r.json()["checks"] if c["check"] == "join_fanout")

    assert fanout()["status"] == "needs_input"
    cand = next(c for c in api.get(f"{base}/relationships/candidates?status=pending", headers=analyst).json()
                if c["from_asset"] == pair[0] and c["to_asset"] == pair[1])
    ok = api.post(f"{base}/relationships/candidates/{cand['id']}/accept", headers=approver, json={"reason": "FK by design"})
    assert ok.status_code == 200, ok.text
    check = fanout()
    assert check["status"] == "pass", check
    suggestion = api.get(f"{base}/model/suggestion", headers=analyst).json()
    rel = next(r for r in suggestion["relationships"] if r["candidate_id"] == cand["id"])
    assert rel["status"] == "validated"
