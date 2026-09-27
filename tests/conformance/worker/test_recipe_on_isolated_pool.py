"""P7-06 first consumer: a recipe snapshot statement on the isolated compute-py pool gives exactly the
in-process result, with inputs and output verified by hash on both sides (ADR-0023 + ADR-0022)."""
from __future__ import annotations

import gzip
import json

import pytest

from analystos.core.errors import FeatureUnavailable
from analystos.recipes.execute import SnapshotStore, run_snapshot_job
from analystos.workers.dispatch import ListSink
from analystos.workers.recipe import recipe_envelope, run_recipe_isolated

SQL = ('SELECT "o"."region" AS "region", SUM("o"."amount") AS "revenue", COUNT(*) AS "orders" '
       'FROM "sales"."orders" AS "o" JOIN "sales"."customers" AS "c" ON "o"."customer_id" = "c"."id" '
       'WHERE "c"."active" GROUP BY "o"."region" ORDER BY "region"')


def _job(tmp_path):
    store = SnapshotStore(tmp_path / "recipe_snapshots")
    orders = store.put(["customer_id", "region", "amount"], [[1, "north", 10.5], [2, "south", 4.0], [1, "north", 2.0],
                                                              [3, "east", 7.25]])
    customers = store.put(["id", "active"], [[1, True], [2, True], [3, False]])
    return {"sql": SQL, "tables": {"sales.orders": orders, "sales.customers": customers}, "max_rows": 100,
            "timeout_seconds": 20, "artifact_dir": str(tmp_path), "workspace_id": "ws_recipe"}, store


def test_a_recipe_snapshot_job_on_compute_py_matches_the_inline_job(tmp_path, store_server, transport):
    job, store = _job(tmp_path)
    inline_dir = tmp_path / "inline"
    inline_store = SnapshotStore(inline_dir / "recipe_snapshots")
    for digest in job["tables"].values():
        inline_store.put(*store.get(digest))
    expected = run_snapshot_job({**job, "artifact_dir": str(inline_dir)})
    sink = ListSink()
    out = run_recipe_isolated(job, transport=transport, sink=sink, secret=store_server.secret, artifacts=store_server.store,
                              settings=type("S", (), {"isolated_pool_set": {"compute-py"}})())
    assert out == expected
    columns, rows = store.get(out["result"])  # materialized into the control plane's snapshot store, verified
    assert columns == ["region", "revenue", "orders"] and rows == [["north", 12.5, 2], ["south", 4.0, 1]]
    result = sink.results[0]
    assert result.status == "completed" and result.usage.cpu_seconds > 0
    ref = result.outputs["result"]
    _, data = store_server.store.read(ref.artifact_id)
    assert json.loads(gzip.decompress(data))["columns"] == columns
    # a second run of the same statement over the same snapshots binds the same output artifact
    again = run_recipe_isolated(job, transport=transport, sink=sink, secret=store_server.secret,
                                artifacts=store_server.store, settings=type("S", (), {"isolated_pool_set": {"compute-py"}})())
    assert again == out and sink.results[1].outputs["result"] == ref


def test_the_envelope_names_every_snapshot_by_artifact_and_content_hash(tmp_path, store_server):
    job, store = _job(tmp_path)
    env = recipe_envelope(job, store, store_server.store)
    assert env.spec.kind == "recipe.snapshot" and env.required_outputs == ["result"]
    assert {t.snapshot for t in env.spec.tables.values()} == set(job["tables"].values())
    assert {r.artifact_id for r in env.input_artifacts} == {t.artifact_id for t in env.spec.tables.values()}
    assert env.idempotency_key == recipe_envelope(job, store, store_server.store).idempotency_key


def test_without_the_pool_the_isolated_path_says_why(tmp_path, store_server, transport):
    job, _ = _job(tmp_path)
    with pytest.raises(FeatureUnavailable, match="ANALYSTOS_ISOLATED_POOLS=compute-py"):
        run_recipe_isolated(job, transport=transport, sink=ListSink(), secret=store_server.secret,
                            artifacts=store_server.store, settings=type("S", (), {"isolated_pool_set": set()})())
