"""P6-04/P6-05/P6-07 on the compose Postgres: a recipe over a staged file source runs through the
query gateway (pushdown, Postgres) and over snapshots (DuckDB) with equal results; a violated join
cardinality is refused; `fail` gates keep the last good output and quarantine the candidate; `drop`
gates quarantine the failing rows, counted and read back through the gateway; column tags follow the
IR's column lineage onto the output; the run's OpenLineage events validate. Skips cleanly without
the stack."""
from __future__ import annotations

import csv
import json
import math
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.unit.recipe_fixtures import EXPECTED, RECIPE, SCHEMA, TABLES

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


def _write_csv(path: Path, table: dict) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(table["columns"])
        for r in table["rows"]:
            w.writerow(["" if v is None else v for v in r])


def _spec(schema: str, **changes) -> dict:
    spec = json.loads(json.dumps(RECIPE).replace(f"{SCHEMA}.", f"{schema}."))
    for node_id, patch in changes.items():
        node = next(n for n in spec["nodes"] if n["id"] == node_id)
        node.update(patch)
    return spec


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Source, User
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    get_settings.cache_clear()
    folder = f"recipes-{new_id('t')}"
    upload = Path(get_settings().upload_dir) / folder
    upload.mkdir(parents=True, exist_ok=True)
    for asset, table in TABLES.items():
        _write_csv(upload / f"{asset.split('.')[1]}.csv", table)
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        ws = create_workspace(s, owner, name=f"recipes {folder}", objective="", autonomy_level=3)
        s.flush()
        src = register_source(s, owner, ws.id, kind="csv", name="orders files", config={"path": folder}, secret_ref=None)
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(owner)
    discover_source(owner, src_id, ws_id)
    select_assets(owner, src_id, ["orders", "legacy_orders", "customers"], ws_id)
    with session_scope() as s:
        schema = s.get(Source, src_id).staging_schema or f"src_{src_id}"
    yield {"ws": ws_id, "src": src_id, "schema": schema, "owner": owner}
    shutil.rmtree(upload, ignore_errors=True)


def _save(world, spec) -> str:
    from analystos.db.base import session_scope
    from analystos.services.recipes import save_recipe

    with session_scope() as s:
        return save_recipe(s, world["owner"], world["ws"], spec).id


def _by_customer(columns, rows) -> dict:
    out = {}
    for r in rows:
        rec = dict(zip(columns, r, strict=False))
        out[rec["customer_id"]] = [rec["region"], rec["orders"], float(rec["revenue_eur"]), rec["best_rank"]]
    return out


def _close(a, b) -> bool:
    return a.keys() == b.keys() and all(
        (math.isclose(float(x), float(y), rel_tol=1e-9) if isinstance(x, float) or isinstance(y, float) else x == y)
        for k in a for x, y in zip(a[k], b[k], strict=True))


def test_same_recipe_equal_through_the_gateway_and_duckdb(world):
    from analystos.db.base import session_scope
    from analystos.db.models import QueryExecution
    from analystos.services.recipes import run_recipe

    rid = _save(world, _spec(world["schema"]))
    via_sql = run_recipe(world["owner"], rid, world["ws"], mode="preview", engine="sql")
    via_duckdb = run_recipe(world["owner"], rid, world["ws"], mode="preview", engine="duckdb")
    auto = run_recipe(world["owner"], rid, world["ws"], mode="preview")
    assert via_sql["plan"]["engine"] == "sql" and via_sql["plan"]["pushdown"] is True
    assert auto["plan"]["engine"] == "sql" and auto["plan"]["fallback_reasons"] == []
    assert via_duckdb["plan"]["engine"] == "duckdb"
    assert via_duckdb["plan"]["fallback_reasons"] == ["the DuckDB snapshot engine was requested"]
    sql_rows = via_sql["preview"]["customer_revenue"]
    duck_rows = via_duckdb["preview"]["customer_revenue"]
    assert sql_rows["columns"] == duck_rows["columns"] == ["customer_id", "region", "orders", "revenue_eur", "best_rank"]
    a, b = _by_customer(sql_rows["columns"], sql_rows["rows"]), _by_customer(duck_rows["columns"], duck_rows["rows"])
    assert _close(a, b) and _close(a, EXPECTED)
    # the join was measured before either ran, and every read was a governed gateway query
    assert via_sql["preflight"][0]["observed"] == "many_to_one" and via_sql["preflight"][0]["ok"]
    assert len(via_duckdb["snapshots"]) == 3
    with session_scope() as s:
        purposes = [q.purpose for q in s.scalars(select(QueryExecution).where(QueryExecution.id.in_(
            via_sql["query_ids"] + via_duckdb["query_ids"])))]
    assert any(p.startswith("recipe.preview:") for p in purposes)
    assert sum(p == "recipe.snapshot" for p in purposes) == 3


def test_a_violated_join_cardinality_is_refused_before_anything_runs(world):
    from analystos.core.errors import InvalidInput
    from analystos.db.base import session_scope
    from analystos.db.models import RecipeRun
    from analystos.services.recipes import run_recipe

    spec = _spec(world["schema"], with_customer={"expected_cardinality": "one_to_one"})
    spec["name"] = "customer_orders_strict_join"
    rid = _save(world, spec)
    for engine in ("sql", "duckdb"):
        with pytest.raises(InvalidInput) as err:
            run_recipe(world["owner"], rid, world["ws"], engine=engine)
        assert "declared one_to_one but the data is many_to_one" in err.value.message
        with session_scope() as s:
            run = s.get(RecipeRun, err.value.details["recipe_run_id"])
            assert run.status == "refused" and run.outputs == {}


def test_gates_keep_last_good_quarantine_and_drop(world):
    from analystos.db.base import session_scope
    from analystos.db.models import LineageEdge, SourceColumn
    from analystos.evidence.schemas import validate_openlineage
    from analystos.runtime.context import default_gateway
    from analystos.services.recipes import quarantine_rows, run_recipe
    from analystos.services.sources import tag_column

    owner, ws, schema = world["owner"], world["ws"], world["schema"]
    with session_scope() as s:
        from analystos.db.models import SourceAsset

        cust = s.scalar(select(SourceAsset).where(SourceAsset.source_id == world["src"], SourceAsset.name == "customers"))
        tag_column(s, owner, cust.id, "region", ["sensitive"])
    # 1. a good run materializes the output through the loader
    good = run_recipe(owner, _save(world, _spec(schema)), ws, engine="sql")
    assert good["status"] == "succeeded", good
    out = good["outputs"]["customer_revenue"]
    assert out["row_count"] == 4 and out["kept_previous"] is False and "quarantine" not in out
    table = out["table"]
    with session_scope() as s:
        from analystos.db.models import RecipeRun

        events = s.get(RecipeRun, good["id"]).openlineage
        tags = {c.name: c.tags for c in s.scalars(select(SourceColumn).join(
            SourceAsset, SourceAsset.id == SourceColumn.asset_id).where(SourceAsset.source_id == out["source_id"],
                                                                         SourceAsset.name == table.split(".")[1]))}
        edges = {(e.from_type, e.from_id, e.relation, e.to_type, e.to_id) for e in s.scalars(
            select(LineageEdge).where(LineageEdge.workspace_id == ws))}
    assert [e["eventType"] for e in events] == ["START", "COMPLETE"]
    assert all(validate_openlineage(e) == [] for e in events)
    assert "'paid'" not in events[1]["job"]["facets"]["sql"]["query"]
    assert "sensitive" in tags["region"] and tags["revenue_eur"] == []  # tags follow the column lineage
    assert ("recipe_run", good["id"], "produced", "table", table) in edges
    assert ("table", f"{schema}.orders", "transformed_into", "table", table) in edges
    # the output is a staged asset: readable through the gateway like any other
    from analystos.governance.policy import resolve_scope

    with session_scope() as s:
        scope = resolve_scope(s, owner, ws)
    runner = default_gateway().run_sql_for(scope, actor="test", source_id=out["source_id"])
    first = runner(f'SELECT COUNT(*) FROM {table}', purpose="test").rows[0][0]
    assert first == 4

    # 2. a failing `fail` gate: the output keeps its last good version, the candidate is quarantined
    blocked = run_recipe(owner, _save(world, _spec(schema, out={"gates": [
        {"type": "range", "column": "revenue_eur", "max": 100, "severity": "fail"}]})), ws, engine="duckdb")
    assert blocked["status"] == "blocked"
    b = blocked["outputs"]["customer_revenue"]
    assert b["kept_previous"] is True and b["table"] == table and b["content_fingerprint"] == out["content_fingerprint"]
    assert b["quarantined_rows"] == 4
    gate = next(g for g in blocked["gates"]["customer_revenue"]["gates"] if g["gate"] == "range(revenue_eur)")
    assert gate["status"] == "failed" and gate["failed_rows"] == 2
    assert runner(f'SELECT COUNT(*) FROM {table}', purpose="test", use_cache=False).rows[0][0] == 4
    q = quarantine_rows(owner, blocked["id"], ws, output="customer_revenue")
    assert q["row_count"] == 4 and all(r[q["columns"].index("aos_reason")].startswith("candidate: range(revenue_eur)")
                                       for r in q["rows"])

    # 3. a `drop` gate: the failing row moves to quarantine, counted and shown; the rest is published
    dropped = run_recipe(owner, _save(world, _spec(schema, out={"gates": [
        {"type": "not_null", "column": "region", "severity": "drop"},
        {"type": "accepted_values", "column": "region", "values": ["north"], "severity": "warn"}]})), ws)
    assert dropped["status"] == "succeeded"
    d = dropped["outputs"]["customer_revenue"]
    assert (d["row_count"], d["dropped_rows"], d["quarantined_rows"]) == (3, 1, 1)
    summary = dropped["gates"]["customer_revenue"]
    assert summary["dropped_rows"] == 1 and summary["warnings"]  # the warn gate is evidence, not a block
    q = quarantine_rows(owner, dropped["id"], ws, output="customer_revenue")
    assert q["row_count"] == 1 and q["rows"][0][q["columns"].index("customer_id")] == 9
    assert runner(f'SELECT COUNT(*) FROM {table}', purpose="test", use_cache=False).rows[0][0] == 3


def test_recipe_api_round_trip(world, control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as api:
        r = api.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": PASSWORD})
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        ws = world["ws"]
        bad = api.post(f"/api/workspaces/{ws}/recipes/validate", headers=h,
                       json={"spec": _spec(world["schema"], paid={"predicate": "status = 1"})})
        assert bad.status_code == 422 and "compares text with numeric" in bad.text
        spec = _spec(world["schema"])
        spec["name"] = "customer_orders_api"
        saved = api.post(f"/api/workspaces/{ws}/recipes", headers=h, json={"spec": spec}).json()
        assert saved["version"] == 1 and saved["status"] == "draft"
        assert api.post(f"/api/workspaces/{ws}/recipes", headers=h, json={"spec": spec}).json()["id"] == saved["id"]
        pub = api.post(f"/api/workspaces/{ws}/recipes/{saved['id']}/publish", headers=h).json()
        assert pub["status"] == "published"
        dbt = api.get(f"/api/workspaces/{ws}/recipes/{saved['id']}/compiled?target=dbt", headers=h).json()
        assert "models/customer_revenue.sql" in dbt["files"]
        sql = api.get(f"/api/workspaces/{ws}/recipes/{saved['id']}/compiled", headers=h).json()
        assert sql["plan"]["engine"] == "sql" and "WITH" in sql["sql"]["customer_revenue"]
        run = api.post(f"/api/workspaces/{ws}/recipes/{saved['id']}/runs", headers=h, json={"mode": "materialize"}).json()
        assert run["status"] == "succeeded", run
        got = api.get(f"/api/workspaces/{ws}/recipe-runs/{run['id']}", headers=h).json()
        assert got["outputs"]["customer_revenue"]["row_count"] == 4
        lin = api.get(f"/api/workspaces/{ws}/recipe-runs/{run['id']}/lineage", headers=h).json()
        assert lin["lineage"][0]["output"] == "customer_revenue" and len(lin["openlineage"]) == 2
        other = api.post("/api/workspaces", headers=h, json={"name": "other"}).json()["id"]
        assert api.get(f"/api/workspaces/{other}/recipe-runs/{run['id']}", headers=h).status_code == 404
        assert api.get(f"/api/workspaces/{other}/recipes/{saved['id']}", headers=h).status_code == 404
        # SQL lineage against the workspace catalog: unqualified columns through a CTE resolve to the staged asset
        schema = world["schema"]
        lin = api.post(f"/api/workspaces/{ws}/lineage/sql", headers=h, json={"sql": (
            f"WITH paid AS (SELECT customer_id, amount FROM {schema}.orders WHERE status = 'paid') "
            "SELECT customer_id, SUM(amount) AS total FROM paid GROUP BY customer_id")}).json()
        by_target = {e["target_column"]: e for e in lin["edges"] if e["transformation_type"] != "FILTERED"}
        assert by_target["total"]["source_table"] == f"{schema}.orders" and by_target["total"]["source_resolved"]
        assert by_target["total"]["transformation_type"] == "AGGREGATED" and lin["confidence"] == "FULL"
        assert "'paid'" not in lin["redacted_sql"]
