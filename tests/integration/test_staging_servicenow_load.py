"""ServiceNow mock -> connector -> staging loader -> analytics DB (loader writes, reader reads)."""
from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataplane_fixtures import *  # noqa: E402,F403
from dataplane_fixtures import reader_as  # noqa: E402

pytestmark = pytest.mark.integration
WS = "ws_dp_loader_test"


def test_servicenow_tables_are_staged(staged_servicenow, dp_settings, dp_workspace) -> None:
    loads = staged_servicenow["loads"]
    assert loads["incident"]["row_count"] == 20_000
    assert loads["change_request"]["row_count"] == 3_000
    assert loads["sys_user_group"]["row_count"] == 12
    assert loads["cmdb_ci"]["row_count"] == 40
    schema = staged_servicenow["schema"]
    with reader_as(dp_settings, dp_workspace["workspace_id"]) as conn:
        types = dict(conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = :s AND table_name = 'incident'"
        ), {"s": schema}).all())
        assert types["opened_at"] == "timestamp without time zone"
        assert types["made_sla"] == "boolean"
        assert types["priority"] == "bigint"
        assert types["assignment_group_name"] == "text"
        top = conn.execute(text(
            f'SELECT assignment_group_name FROM "{schema}".incident WHERE assignment_group_name IS NOT NULL '
            "GROUP BY 1 ORDER BY AVG(reassignment_count) DESC LIMIT 1"
        )).scalar_one()
        assert top == "Network Operations"
        leftovers = conn.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s AND table_name LIKE '%\\_\\_load'"
        ), {"s": schema}).scalar_one()
        assert leftovers == 0


def test_reload_swaps_atomically(dp_settings) -> None:
    from analystos.staging.loader import StagingLoader

    loader = StagingLoader(dp_settings)
    sid = "dp_reload_test"
    try:
        schema_tbl = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "Label Name": pa.array(["a", "b", None])})
        r1 = loader.load(sid, "My Table!", iter(schema_tbl.to_batches()), workspace_id=WS)
        assert r1 == {"row_count": 3, "schema": "src_dp_reload_test", "table": "my_table",
                      "columns": [{"name": "id", "type": "bigint"}, {"name": "label_name", "type": "text"}]}
        r2 = loader.load(sid, "My Table!", iter(pa.table({"id": [9], "Label Name": ["z"]}).to_batches()), workspace_id=WS)
        assert r2["row_count"] == 1
        with reader_as(dp_settings, WS) as conn:
            assert conn.execute(text("SELECT id, label_name FROM src_dp_reload_test.my_table")).all() == [(9, "z")]
        # A failing load leaves the previous snapshot intact.
        def bad_batches():
            yield pa.record_batch({"id": pa.array([1], pa.int64()), "Label Name": pa.array(["x"])})
            raise RuntimeError("source died mid-extract")

        with pytest.raises(RuntimeError):
            loader.load(sid, "My Table!", bad_batches(), workspace_id=WS)
        with reader_as(dp_settings, WS) as conn:
            assert conn.execute(text("SELECT count(*) FROM src_dp_reload_test.my_table")).scalar_one() == 1
    finally:
        loader.drop_source(sid)
    with reader_as(dp_settings, None) as conn:
        assert conn.execute(text("SELECT count(*) FROM pg_namespace WHERE nspname = 'src_dp_reload_test'")).scalar_one() == 0


def test_loader_handles_nested_and_decimal_types(dp_settings) -> None:
    from decimal import Decimal

    from analystos.staging.loader import StagingLoader

    loader = StagingLoader(dp_settings)
    sid = "dp_types_test"
    try:
        t = pa.table({
            "amount": pa.array([Decimal("1.25"), None], pa.decimal128(10, 2)),
            "tags": pa.array([["a", "b"], []], pa.list_(pa.string())),
            "ts": pa.array([1_700_000_000_000_000, None], pa.timestamp("us", tz="UTC")),
        })
        assert loader.load(sid, "types", iter(t.to_batches()), workspace_id=WS)["row_count"] == 2
        with reader_as(dp_settings, WS) as conn:
            row = conn.execute(text("SELECT amount, tags, ts FROM src_dp_types_test.types WHERE amount IS NOT NULL")).one()
            assert row[0] == Decimal("1.25") and row[1] == ["a", "b"] and row[2].year == 2023
    finally:
        loader.drop_source(sid)
