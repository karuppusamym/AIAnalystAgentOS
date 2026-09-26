"""P7-09: composite keys and composite foreign keys, measured through the gateway.

The first half is Atlas's composite-key suite as the spec
(AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:tests/test_composite_key_inference.py), rewritten
for measurement: Atlas scores guesses from single-column statistics; here a candidate is reported only
when COUNT(DISTINCT (a, b)) over the real rows equals the row count. What carries over unchanged is the
search bounds (key size, member pool, combinations, results), the exclusions (declared keys, nullable
members, low-cardinality members) and "no evidence, no candidate". Where Atlas surfaces a non-minimal
guess (a pair containing an already-unique column), measurement reports the minimal key instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.contracts.policy import DataScope  # noqa: E402
from analystos.core.errors import SQLRejected  # noqa: E402
from analystos.skills.relationships import (  # noqa: E402
    MAX_CANDIDATE_MEMBERS,
    MAX_CANDIDATES_RETURNED,
    MAX_KEY_SIZE,
    ColumnKeyStats,
    discover_composite_relationships,
    discover_keys,
    key_combinations,
    key_member_pool,
    measure_columns,
    with_assessment,
)
from skills_fixtures import DuckRunSQL, GatewayRunSQL, TsqlViaDuckRunSQL  # noqa: E402


def _asset(name, cols, keys=()):
    return {"asset": name, "columns": [{"name": c, "data_type": "INTEGER", "is_key": c in keys} for c in cols]}


@pytest.fixture
def duck():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA ops")
    # order lines: (order_id, line_no) is the key; neither column is unique alone
    con.execute("CREATE TABLE ops.lines AS SELECT o AS order_id, l AS line_no, l % 2 AS status_flag, o * 100 + l AS line_uid "
                "FROM range(1, 51) t(o), range(1, 4) u(l)")
    # shipments reference (order_id, line_no); one line is shipped twice (many shipments per line)
    con.execute("CREATE TABLE ops.shipments AS SELECT order_id, line_no, row_number() OVER () AS shipment_no FROM ops.lines "
                "UNION ALL SELECT 1, 1, 999")
    # a table with no unique combination at all
    con.execute("CREATE TABLE ops.events AS SELECT i % 3 AS a, i % 2 AS b FROM range(0, 30) t(i)")
    con.execute("CREATE TABLE ops.empty (x INTEGER, y INTEGER)")
    run = DuckRunSQL(con)
    yield run
    con.close()


# --------------------------------------------------------------------------- Atlas bounds as the spec
def test_clean_two_column_composite_key_is_measured_and_bounded(duck):
    keys = discover_keys(duck, _asset("ops.lines", ["order_id", "line_no", "status_flag"]))
    assert [k.columns for k in keys] == [["order_id", "line_no"]]
    pair = keys[0]
    assert pair.rows == pair.distinct_tuples == 150 and 0 < pair.confidence <= 1
    assert pair.evidence["sql"].count("DISTINCT") == 1 and {c["name"] for c in pair.evidence["columns"]} == {"order_id", "line_no"}
    for c in pair.evidence["columns"]:
        assert {"non_null", "distinct", "distinct_ratio"} <= set(c)


def test_single_column_key_is_the_size_one_case_and_supersets_are_not_keys(duck):
    keys = discover_keys(duck, _asset("ops.lines", ["order_id", "line_no", "line_uid"]))
    assert ["line_uid"] in [k.columns for k in keys]
    assert all("line_uid" not in k.columns or k.columns == ["line_uid"] for k in keys)  # minimal keys only
    narrow = next(k for k in keys if k.columns == ["line_uid"])
    wide = next(k for k in keys if len(k.columns) == 2)
    assert narrow.confidence > wide.confidence  # narrower keys rank higher (Atlas's per-member discount)


def test_declared_key_columns_are_excluded(duck):
    keys = discover_keys(duck, _asset("ops.lines", ["order_id", "line_no", "line_uid"], keys=("line_uid",)))
    assert all("line_uid" not in k.columns for k in keys) and [k.columns for k in keys] == [["order_id", "line_no"]]


def test_column_with_nulls_above_threshold_is_excluded():
    nully = ColumnKeyStats(name="optional_code", rows=1000, non_null=950, distinct=950)
    other = ColumnKeyStats(name="sequence_no", rows=1000, non_null=1000, distinct=1000)
    assert [s.name for s in key_member_pool([nully, other])] == ["sequence_no"]


def test_low_cardinality_members_are_never_measured_alone_or_in_impossible_pairs():
    country = ColumnKeyStats(name="country", rows=1000, non_null=1000, distinct=5)
    flag = ColumnKeyStats(name="flag", rows=1000, non_null=1000, distinct=2)
    assert key_combinations([country, flag], 1000) == []  # 5 x 2 < 1000: no combination can be unique


def test_no_rows_or_no_columns_produce_nothing(duck):
    assert discover_keys(duck, _asset("ops.empty", ["x", "y"])) == []
    assert discover_keys(duck, {"asset": "ops.empty", "columns": []}) == []


def test_many_to_many_table_has_no_key(duck):
    assert discover_keys(duck, _asset("ops.events", ["a", "b"])) == []


def test_search_stays_bounded_on_a_wide_table():
    stats = [ColumnKeyStats(name=f"col_{i:02d}", rows=1000, non_null=1000, distinct=900 + i) for i in range(40)]
    pool = key_member_pool(stats)
    assert len(pool) == MAX_CANDIDATE_MEMBERS and {s.name for s in pool} == {f"col_{i}" for i in range(32, 40)}
    combos = key_combinations(pool, 1000)
    assert all(len(c) <= MAX_KEY_SIZE for c in combos) and len(combos) <= 200


def test_query_budget_and_result_cap(duck):
    keys = discover_keys(duck, _asset("ops.lines", ["order_id", "line_no", "status_flag", "line_uid"]), max_queries=1)
    assert [k.columns for k in keys] == [["line_uid"]]  # the stats statement alone measures single columns
    assert len(keys) <= MAX_CANDIDATES_RETURNED


# --------------------------------------------------------------------------- composite foreign keys
def test_composite_fk_is_detected_by_measurement(duck):
    assets = [_asset("ops.lines", ["order_id", "line_no", "status_flag"]), _asset("ops.shipments", ["order_id", "line_no", "shipment_no"])]
    rels = discover_composite_relationships(duck, assets)
    r = next(r for r in rels if r.from_asset == "ops.shipments")
    assert (r.from_columns, r.to_asset, r.to_columns) == (["order_id", "line_no"], "ops.lines", ["order_id", "line_no"])
    assert r.cardinality == "many_to_one" and r.evidence["containment"] == 1.0 and r.evidence["target_unique"] is True
    assert r.assessment["outcome"] == "corroborated" and r.assessment["cardinality"] == "many_to_one"
    assert not [r for r in rels if r.from_asset == "ops.lines"]  # shipments has no composite key to reference
    back = with_assessment(measure_columns(duck, "duckdb", "ops.lines", ["order_id", "line_no"], "ops.shipments",
                                           ["order_id", "line_no"], "composite_key"), assets)
    assert back.cardinality == "one_to_many" and "DIRECTION_REVERSED" in back.assessment["warnings"]


def test_composite_measurement_runs_through_the_gateway_and_in_tsql(duck):
    scope = DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"], assets=["ops.lines", "ops.shipments"],
                      asset_sources={"ops.lines": "s", "ops.shipments": "s"}, source_dialects={"s": "duckdb"},
                      columns={"ops.lines": ["order_id", "line_no", "status_flag", "line_uid"],
                               "ops.shipments": ["order_id", "line_no", "shipment_no"]})
    gw = GatewayRunSQL(duck, scope)
    assets = [_asset("ops.lines", ["order_id", "line_no"]), _asset("ops.shipments", ["order_id", "line_no"])]
    rels = discover_composite_relationships(gw, assets, keys={"ops.lines": [["order_id", "line_no"]]})
    assert [(r.from_asset, r.cardinality) for r in rels] == [("ops.shipments", "many_to_one")]
    assert gw.calls and all(c["executable_sql"] for c in gw.calls)
    tsql = discover_composite_relationships(TsqlViaDuckRunSQL(duck), assets, keys={"ops.lines": [["order_id", "line_no"]]})
    assert [(r.from_asset, r.cardinality) for r in tsql] == [("ops.shipments", "many_to_one")]
    scope.denied_columns = ["ops.shipments.line_no"]
    with pytest.raises(SQLRejected):
        discover_composite_relationships(gw, assets, keys={"ops.lines": [["order_id", "line_no"]]})
