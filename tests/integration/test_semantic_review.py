"""P7-09 / P4-05 / P7-02 on the real stack: relationship candidates measured through the gateway land in
the review queue; separation of duties and the hash-bound approval decide them; the accepted join carries
its measured cardinality; a governed Ask joins over it (and refuses the fan-out direction); policy row
filters with user attributes apply in the compiler and fail closed; agent-proposed structure is approved
by someone else; and a structure change that breaks an approved metric deprecates it."""
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
    """A workspace (analyst owner, approver member) over two staged tables: customers and orders."""
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, User
    from analystos.services.sources import discover_source, register_source, select_assets

    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    r = api.post("/api/workspaces", headers=analyst, json={"name": "semantic review", "objective": "Governed joins"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "approver"}).status_code == 200
    folder = get_settings().upload_dir / ws / "shop"
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"customer_id": [1, 2, 3, 4], "segment": ["Enterprise", "SMB", "SMB", "Enterprise"],
                  "region": ["EU", "EU", "US", "US"]}).to_parquet(folder / "customers.parquet", index=False)
    pd.DataFrame({"order_id": list(range(1, 9)), "customer_id": [1, 1, 2, 3, 3, 3, 4, 4],
                  "amount": [10.0, 20.0, 5.0, 1.0, 2.0, 3.0, 100.0, 50.0]}).to_parquet(folder / "orders.parquet", index=False)
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, user, ws, kind="csv", name="shop", config={"path": f"{ws}/shop"}, secret_ref=None)
        s.flush()
        src_id = src.id
        s.expunge(user)
    discover_source(user, src_id)
    select_assets(user, src_id, ["customers", "orders"])
    with session_scope() as s:
        assets = {a.name: f"{a.schema_name}.{a.name}" for a in s.scalars(select(SourceAsset).where(SourceAsset.workspace_id == ws))}
    return {"ws": ws, "analyst": analyst, "approver": approver, "assets": assets, "base": f"/api/workspaces/{ws}/semantic"}


def _users():
    from analystos.db.base import session_scope
    from analystos.db.models import User

    with session_scope() as s:
        out = {u.email.split("@")[0]: u for u in s.scalars(select(User).where(User.email.like("%@analystos.local")))}
        s.expunge_all()
    return out


def test_candidates_are_measured_and_decided_with_separation_of_duties(api, env):
    base, analyst, approver = env["base"], env["analyst"], env["approver"]
    orders, customers = env["assets"]["orders"], env["assets"]["customers"]
    r = api.post(f"{base}/relationships/discover", headers=analyst, json={})
    assert r.status_code == 200, r.text
    cand = next(c for c in r.json() if c["from_asset"] == orders and c["to_asset"] == customers)
    assert cand["status"] == "pending" and cand["cardinality"] == "many_to_one" and cand["containment"] == 1.0
    assert cand["assessment"]["outcome"] == "corroborated" and cand["evidence"]["target_unique"] is True
    # cardinality is never taken from a request: the proposal body has no such field
    bad = api.post(f"{base}/relationships/candidates", headers=analyst,
                   json={"from_asset": orders, "from_columns": ["customer_id"], "to_asset": customers,
                         "to_columns": ["customer_id"], "cardinality": "one_to_one"})
    assert bad.status_code == 422
    # the person who measured it cannot accept it, through either door
    own = api.post(f"{base}/relationships/candidates/{cand['id']}/accept", headers=analyst)
    assert own.status_code == 403
    assert api.post(f"/api/approvals/{cand['approval_id']}/approve", headers=analyst).status_code == 403
    ok = api.post(f"{base}/relationships/candidates/{cand['id']}/accept", headers=approver, json={"reason": "FK by design"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "accepted" and ok.json()["relationship_name"]
    model = api.get(base, headers=analyst).json()["model"]
    rel = next(x for x in model["relationships"] if x["name"] == ok.json()["relationship_name"])
    assert (rel["cardinality"], rel["validated_by"]) == ("many_to_one", _users()["approver"].id) and rel["validated_at"]
    assert model["status"] == "approved" and {d["source"] for d in model["datasets"]} >= {orders, customers}
    reverse = api.post(f"{base}/relationships/candidates", headers=analyst,
                       json={"from_asset": customers, "from_columns": ["customer_id"], "to_asset": orders, "to_columns": ["customer_id"]})
    assert reverse.status_code == 200 and reverse.json()["cardinality"] == "one_to_many"
    assert "DIRECTION_REVERSED" in reverse.json()["assessment"]["warnings"]
    listed = api.get(f"{base}/relationships/candidates?status=accepted", headers=analyst).json()
    assert [c["id"] for c in listed] == [cand["id"]]


def test_governed_ask_joins_refuses_fan_out_and_applies_row_filters(api, env):
    from analystos.db.base import session_scope
    from analystos.db.models import User, Workspace
    from analystos.governance.policy import load_policy, save_policy
    from analystos.services.ask import ask_in_thread, create_thread

    base, analyst, approver, ws = env["base"], env["analyst"], env["approver"], env["ws"]
    model = api.get(base, headers=analyst).json()["model"]
    orders_ds = next(d["name"] for d in model["datasets"] if d["source"] == env["assets"]["orders"])
    customers_ds = next(d["name"] for d in model["datasets"] if d["source"] == env["assets"]["customers"])
    body = {"name": "sales_amount", "expression": "SUM(amount)", "dataset": orders_ds, "display_name": "Sales amount",
            "dimensions": [f"{customers_ds}.segment"]}
    diff = api.post(f"{base}/metrics/validate", headers=analyst, json=body).json()["diff"]
    assert diff["base_version"] is None and {e["field"] for e in diff["entries"]} >= {"expression", "dataset"}
    assert api.post(f"{base}/metrics", headers=analyst, json=body).status_code == 200
    assert api.get(f"{base}/metrics/sales_amount/diff", headers=analyst).json()["has_changes"] is True
    assert api.post(f"{base}/metrics/sales_amount/approve", headers=approver).status_code == 200
    # the segment field needs a dimension flag for the compiler: a person edits the structure (approved)
    with session_scope() as s:
        from analystos.contracts.semantic import SemanticDataset
        from analystos.semantic import service as semantic

        cur = semantic.current_model(s, ws)
        ds = next(d for d in cur.datasets if d["name"] == customers_ds)
        for f in ds["fields"]:
            if f["name"] in ("segment", "region"):
                f["dimension"] = {"is_time": False}
        analyst_user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        semantic.save_model(s, ws, actor=f"user:{analyst_user.id}", origin="user", datasets=[SemanticDataset.model_validate(ds)])
        s.expunge(analyst_user)

    def ask(user, question, parameters=None):
        with session_scope() as s:
            thread = create_thread(s, s.merge(user), ws, title="governed")["id"]
        return ask_in_thread(user, thread, question, parameters)

    turn = ask(analyst_user, "sales amount by segment")
    assert turn["status"] == "answered" and turn["governance"] == "governed", (turn.get("refusal"), turn.get("stages"), turn.get("sql"), turn.get("answered_by"))
    assert sorted(map(tuple, turn["result"]["rows"])) == [("Enterprise", 180.0), ("SMB", 11.0)]
    assert turn["semantic_model_version"] and turn["compiler_version"] == "semantic.v2"
    assert turn["provenance"]["semantic"]["joins"][0]["cardinality"] == "many_to_one"
    # row filter on customers by the caller's regions: applied in the compiler, fail closed without the attribute
    with session_scope() as s:
        w = s.get(Workspace, ws)
        policy = load_policy(s, w)
        policy.row_filters = [{"id": "by_region", "assets": [env["assets"]["customers"]], "predicate": "region IN {{user.regions}}"}]
        save_policy(s, w, type(policy).model_validate(policy.model_dump()), analyst_user.id)
        s.get(User, analyst_user.id).attributes = {**(s.get(User, analyst_user.id).attributes or {}), "regions": ["EU"]}
    analyst_user = _users()["analyst"]  # the stored attributes, not a stale copy
    turn = ask(analyst_user, "sales amount by segment")
    assert turn["governance"] == "governed" and "'EU'" in turn["sql"]
    # orders of US customers stay (the fact table is not filtered) but their segment is not visible
    assert sorted(map(tuple, turn["result"]["rows"]), key=repr) == sorted([("Enterprise", 30.0), ("SMB", 5.0), (None, 156.0)], key=repr)
    assert turn["provenance"]["semantic"]["row_filtered_assets"] == [env["assets"]["customers"]]
    with session_scope() as s:
        s.get(User, analyst_user.id).attributes = {k: v for k, v in (s.get(User, analyst_user.id).attributes or {}).items()
                                                   if k != "regions"}
    analyst_user = _users()["analyst"]
    refused = ask(analyst_user, "sales amount by segment")
    assert refused["status"] == "refused" and "regions" in (refused["refusal"] or {}).get("message", ""), refused
    with session_scope() as s:
        w = s.get(Workspace, ws)
        policy = load_policy(s, w)
        policy.row_filters = []
        save_policy(s, w, policy, analyst_user.id)
    # a dimension the metric was not approved for is refused, whoever asks
    fan = ask(analyst_user, "q", {"semantic_query": {"metrics": ["sales_amount"], "dimensions": [f"{customers_ds}.region"]}})
    assert fan["status"] == "refused" and "not approved" in fan["refusal"]["message"]
    # an additive metric on customers grouped by an orders field crosses the join in its one_to_many direction:
    # refused at approval, naming the edge and the fix
    fan_body = {"name": "customer_rows", "expression": "COUNT(*)", "dataset": customers_ds, "dimensions": [f"{orders_ds}.order_id"]}
    assert api.post(f"{base}/metrics", headers=analyst, json=fan_body).status_code == 200
    refused = api.post(f"{base}/metrics/customer_rows/approve", headers=approver)
    assert refused.status_code == 422 and "one_to_many" in refused.text and "pre_aggregations" in refused.text
    recon = api.get(f"{base}/reconciliation", headers=analyst).json()
    assert {(f["metric"], f["status"]) for f in recon["fanout"]} == {("sales_amount", "ok"), ("customer_rows", "fan_out")}
    assert [x["metric"] for x in recon["invalid_proposals"]] == ["customer_rows"] and recon["stale"] == []
    assert recon["unvalidated_relationships"] == []


def test_agent_structure_needs_another_person_and_stale_metrics_are_deprecated(api, env):
    from analystos.contracts.semantic import SemanticDataset
    from analystos.db.base import session_scope
    from analystos.semantic import service as semantic

    base, analyst, approver, ws = env["base"], env["analyst"], env["approver"], env["ws"]
    users = _users()
    orders_ds = next(d for d in api.get(base, headers=analyst).json()["model"]["datasets"]
                     if d["source"] == env["assets"]["orders"])
    shrunk = {**orders_ds, "fields": [f for f in orders_ds["fields"] if f["name"] != "amount"]}
    with session_scope() as s:
        row = semantic.save_model(s, ws, actor=f"user:{users['analyst'].id}", origin="agent:semantic",
                                  datasets=[SemanticDataset.model_validate(shrunk)])
        version = row.version
        assert row.status == "proposed"
    assert api.get(f"{base}/model/diff?version={version}", headers=analyst).json()["entries"]
    assert api.post(f"{base}/model/approve", headers=analyst, json={"version": version}).status_code == 403
    r = api.post(f"{base}/model/approve", headers=approver, json={"version": version})
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    # the approved structure no longer has the column sales_amount reads: the metric is deprecated, with the reason
    versions = api.get(f"{base}/metrics/sales_amount", headers=analyst).json()
    assert versions[-1]["status"] == "deprecated" and "stale after semantic model" in versions[-1]["reason"]
    assert "sales_amount" not in api.get(base, headers=analyst).json()["approved"]
