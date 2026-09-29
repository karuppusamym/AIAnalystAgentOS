"""N-11: index / partitioning / clustering advice is deterministic, evidence-bound and never executable.

The advisor (skills/index_advice.py) reads query fingerprints, dry-plan scan nodes and crawler stats; every
statement it writes is text for a person, and the gateway validator rejects each one."""
from __future__ import annotations

import pytest

from analystos.contracts.policy import DataScope
from analystos.core.errors import SQLRejected
from analystos.gateway.service import plan_scans
from analystos.gateway.validator import validate_sql
from analystos.skills import index_advice as adv

ASSETS = ["stg.incident", "stg.cmdb_ci"]
COLUMNS = {"stg.incident": ["number", "priority", "category", "opened_at", "cmdb_ci", "caller_id"],
           "stg.cmdb_ci": ["sys_id", "name"]}
SLOW = ("SELECT c.name, COUNT(*) AS n FROM incident i JOIN cmdb_ci c ON c.sys_id = i.cmdb_ci "
        "WHERE i.priority = 1 AND i.category = 'Network' AND i.opened_at >= '2025-01-01' GROUP BY c.name ORDER BY n")


def _stat(fp: str, sql: str, *, count: int = 4, avg: float = 900.0, source: str = "src_a",
          dialect: str = "postgres") -> adv.QueryStat:
    return adv.QueryStat(fingerprint=fp, source_id=source, dialect=dialect, sql=sql, count=count, avg_ms=avg,
                         max_ms=avg * 2, query_ids=[f"qry_{fp}_{i}" for i in range(count)])


def _big(**distinct: int) -> adv.AssetStats:
    return adv.AssetStats(row_count=2_000_000, distinct=dict(distinct),
                          types={"opened_at": "timestamp", "priority": "integer", "category": "text"})


def _recommend(stats, *, plans=None, assets=None, dialect="postgres", **kw):
    uses = {s.fingerprint: adv.column_uses(s.sql, s.dialect, ASSETS, COLUMNS, {"stg.incident.caller_id"}) for s in stats}
    return adv.recommend(stats, uses, plans or {}, assets or {}, {"src_a": dialect},
                         {a: "src_a" for a in ASSETS}, **kw)


# ------------------------------------------------------------------------------ parsing
def test_column_uses_resolves_aliases_roles_and_scope_only():
    uses = adv.column_uses(SLOW, "postgres", ASSETS, COLUMNS)
    assert ("stg.incident", "priority", "eq") in uses
    assert ("stg.incident", "category", "eq") in uses
    assert ("stg.incident", "opened_at", "range") in uses
    assert ("stg.cmdb_ci", "sys_id", "join") in uses and ("stg.incident", "cmdb_ci", "join") in uses
    assert ("stg.cmdb_ci", "name", "group") in uses
    # a table outside the caller's scope contributes nothing, and a denied column is never named
    assert adv.column_uses("SELECT * FROM hr.salaries WHERE grade = 3", "postgres", ASSETS, COLUMNS) == []
    denied = adv.column_uses("SELECT number FROM incident WHERE caller_id = 'x'", "postgres", ASSETS, COLUMNS,
                             {"stg.incident.caller_id"})
    assert denied == []
    assert adv.column_uses("SELEC nonsense ((", "postgres", ASSETS, COLUMNS) == []


def test_plan_scans_keep_column_names_never_literal_values():
    plan = [{"Plan": {"Node Type": "Hash Join", "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "incident", "Plan Rows": 12, "Total Cost": 4000.0,
         "Filter": "((priority = 1) AND ((category)::text = 'Network'::text))"},
        {"Node Type": "Index Scan", "Relation Name": "cmdb_ci", "Index Name": "cmdb_ci_pkey",
         "Index Cond": "(sys_id = i.cmdb_ci)"}]}}]
    scans = plan_scans(plan)
    assert scans[0]["node"] == "Seq Scan" and scans[0]["filter_columns"] == ["category", "priority"]
    assert scans[1]["index"] == "cmdb_ci_pkey" and "sys_id" in scans[1]["index_columns"]
    assert "Network" not in repr(scans)


# ------------------------------------------------------------------------------ recommendations
def test_composite_index_with_plan_evidence_and_benefit():
    stats = [_stat("fp1", SLOW)]
    plans = {"fp1": [{"node": "Seq Scan", "relation": "incident", "index": None, "filter_columns": ["category", "priority"],
                      "index_columns": [], "estimated_rows": 12, "total_cost": 4000.0}]}
    recs, _ = _recommend(stats, plans=plans, assets={"stg.incident": _big(priority=5, category=40, opened_at=900_000),
                                                     "stg.cmdb_ci": _big(sys_id=2_000_000)})
    top = recs[0]
    assert top["kind"] == "index" and top["asset"] == "stg.incident"
    # equality columns first (most selective first), then one range column
    assert top["columns"] == ["category", "priority", "opened_at"]
    assert top["confidence"] == "high"
    assert top["evidence"]["queries"][0] == {"fingerprint": "fp1", "executions": 4, "avg_ms": 900.0, "max_ms": 1800.0,
                                             "query_ids": ["qry_fp1_0", "qry_fp1_1", "qry_fp1_2", "qry_fp1_3"]}
    assert top["evidence"]["plan_nodes"][0]["node"] == "Seq Scan"
    benefit = top["estimated_benefit"]
    assert 0 < benefit["saving_ms_in_window"] <= benefit["query_ms_in_window"] == 3600.0
    assert top["ddl"].startswith("-- Advisory only") and "CREATE INDEX CONCURRENTLY" in top["ddl"]
    # the composite's leading column is not recommended again on its own
    assert not any(r["columns"] == ["category"] for r in recs)
    # the join key gets its own single-column advice; the key is stable across runs
    assert any(r["asset"] == "stg.incident" and r["columns"] == ["cmdb_ci"] for r in recs)
    again, _ = _recommend(stats, plans=plans, assets={"stg.incident": _big(priority=5, category=40, opened_at=900_000),
                                                      "stg.cmdb_ci": _big(sys_id=2_000_000)})
    assert [r["key"] for r in again] == [r["key"] for r in recs]


def test_small_tables_low_selectivity_and_covered_columns_are_skipped_with_reasons():
    stats = [_stat("fp1", "SELECT number FROM incident WHERE priority = 1"),
             _stat("fp2", "SELECT name FROM cmdb_ci WHERE sys_id = 'x'")]
    small = {"stg.cmdb_ci": adv.AssetStats(row_count=500), "stg.incident": _big(priority=5)}
    recs, skipped = _recommend(stats, assets=small)
    assert recs == []
    reasons = {tuple(s["columns"]): s["reason"] for s in skipped}
    assert "cheap" in reasons[("sys_id",)]
    assert "5 distinct values" in reasons[("priority",)]
    covered = {"fp1": [{"node": "Index Scan", "relation": "incident", "index": "ix_prio", "filter_columns": [],
                        "index_columns": ["priority"], "estimated_rows": 3, "total_cost": 8.0}]}
    recs, skipped = _recommend(stats[:1], plans=covered, assets={"stg.incident": _big(priority=900_000)})
    assert recs == [] and "ix_prio" in skipped[0]["reason"]


def test_fast_history_or_no_filters_gives_no_advice():
    recs, skipped = _recommend([_stat("fp1", "SELECT number FROM incident ORDER BY number")])
    assert recs == [] and skipped == []


def test_engines_without_indexes_get_clustering_and_unknown_engines_nothing():
    stats = [_stat("fp1", "SELECT number FROM incident WHERE priority = 1 AND opened_at >= '2025-01-01'",
                   dialect="snowflake")]
    recs, _ = _recommend(stats, dialect="snowflake", assets={"stg.incident": _big(priority=900, opened_at=10**6)})
    assert [r["kind"] for r in recs] == ["clustering"]
    assert "CLUSTER BY" in recs[0]["ddl"] and set(recs[0]["columns"]) == {"priority", "opened_at"}
    recs, skipped = _recommend([_stat("fp1", "SELECT number FROM incident WHERE priority = 1", dialect="trino")],
                               dialect="trino")
    assert recs == [] and "no physical-design advice for trino" in skipped[0]["reason"]


def test_large_table_with_date_range_gets_partition_advice():
    stats = [_stat("fp1", "SELECT number FROM incident WHERE opened_at >= '2025-01-01'")]
    huge = {"stg.incident": adv.AssetStats(row_count=50_000_000, distinct={"opened_at": 40_000_000},
                                           types={"opened_at": "timestamp with time zone"})}
    recs, _ = _recommend(stats, assets=huge)
    kinds = {r["kind"]: r for r in recs}
    assert set(kinds) == {"index", "partition"}
    assert "PARTITION BY RANGE" in kinds["partition"]["ddl"]


def test_staged_assets_carry_the_staging_note():
    stats = [_stat("fp1", "SELECT number FROM incident WHERE category = 'x'")]
    staged = {"stg.incident": adv.AssetStats(row_count=100_000, distinct={"category": 5_000}, execution_mode="staged")}
    recs, _ = _recommend(stats, assets=staged)
    assert any("Staged copy" in n for n in recs[0]["notes"])


def test_unsafe_identifiers_never_reach_ddl():
    with pytest.raises(ValueError):
        adv.ddl("index", "postgres", 'stg.incident"; DROP TABLE x; --', ["priority"])
    with pytest.raises(ValueError):
        adv.ddl("index", "postgres", "stg.incident", ["priority) ; DROP TABLE x; --"])


# ------------------------------------------------------------------------------ never executable
KINDS = [("index", d) for d in sorted(adv.INDEX_DIALECTS)] + [("clustering", d) for d in sorted(adv.CLUSTER_DIALECTS)] + \
        [("partition", d) for d in sorted(adv.PARTITION_DIALECTS)]


@pytest.mark.parametrize(("kind", "dialect"), KINDS)
def test_the_gateway_validator_rejects_every_advice_statement(kind, dialect):
    statement = adv.ddl(kind, dialect, "stg.incident", ["priority", "opened_at"] if kind != "partition" else ["opened_at"])
    scope = DataScope(workspace_id="ws_1", user_id="usr_1", role="owner", source_ids=["src_a"], assets=ASSETS,
                      asset_sources={a: "src_a" for a in ASSETS}, columns=COLUMNS, source_dialects={"src_a": dialect})
    with pytest.raises(SQLRejected):
        validate_sql(scope, statement, max_rows=10)
    # also without the advisory comment header
    with pytest.raises(SQLRejected):
        validate_sql(scope, statement.split("\n")[-1], max_rows=10)


def test_the_api_offers_no_apply_route():
    from fastapi.routing import APIRoute

    from analystos.api.routers.index_advice import router

    routes = {(sorted(r.methods)[0], r.path) for r in router.routes if isinstance(r, APIRoute)}
    assert routes == {("GET", "/api/workspaces/{workspace_id}/index-advice"),
                      ("POST", "/api/workspaces/{workspace_id}/index-advice/analyze"),
                      ("PATCH", "/api/workspaces/{workspace_id}/index-advice/{advice_id}")}


def test_the_service_only_asks_the_gateway_for_plans():
    """The advisor's one gateway call is `explain` (dry, validated, audited); it has no execute path."""
    import inspect

    from analystos.services import index_advice as svc

    source = inspect.getsource(svc)
    assert "gateway.explain(" in source
    assert ".execute(" not in source.replace("session.execute(", "")
