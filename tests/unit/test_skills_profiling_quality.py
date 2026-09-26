"""Profiling + data-quality checks detect the planted defects (duckdb, tsql-via-duckdb, postgres)."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.skills.profiling import AssetProfile, infer_semantic_type, profile_asset, type_family  # noqa: E402
from analystos.skills.quality import check_quality, temporal_pairs  # noqa: E402
from skills_fixtures import (  # noqa: E402
    INCIDENT_COLUMNS,
    NOW,
    PG_DSN,
    PgRunSQL,
    TsqlViaDuckRunSQL,
    duck_dataset,
    pg_available,
    pg_load,
)

RELS = [{"from_asset": "itsm.incident", "from_column": "assignment_group", "to_asset": "itsm.sys_user_group",
         "to_column": "sys_id"}]


@pytest.fixture(scope="module")
def duck():
    return duck_dataset()


@pytest.fixture(scope="module")
def profile(duck) -> AssetProfile:
    duck.calls.clear()
    return profile_asset(duck, "itsm.incident", INCIDENT_COLUMNS)


def test_profile_counts_and_types(profile, duck):
    assert profile.row_count == 6000
    c = {p.name: p for p in profile.columns}
    assert c["sys_id"].semantic_type == "id" and c["number"].semantic_type == "id"
    assert c["assignment_group"].semantic_type == "id"  # declared reference
    assert c["opened_at"].semantic_type == "datetime" and len(c["opened_at"].monthly_counts) == 18
    assert sum(m["count"] for m in c["opened_at"].monthly_counts) == 6000
    assert c["priority"].semantic_type == "categorical" and c["priority"].top_values[0]["value"] == "3 - Moderate"
    assert c["sla_breached"].semantic_type == "boolean" and c["sla_breached"].true_count == 1122
    assert c["reassignment_count"].semantic_type == "numeric" and c["reassignment_count"].percentiles["p50"] == 1.0
    nv = c["noise_value"]
    assert nv.mean == pytest.approx(50, abs=1) and nv.stddev == pytest.approx(10, abs=0.5)
    assert sum(h["count"] for h in nv.histogram) == 6000 and len(nv.histogram) == 20
    assert nv.outliers["low_count"] + nv.outliers["high_count"] > 0
    assert c["u_legacy_code"].null_rate == pytest.approx(0.9193, abs=1e-3)
    assert c["resolved_at"].null_count == 281


def test_profile_query_budget(profile):
    n_dt = sum(1 for p in profile.columns if p.semantic_type == "datetime")
    n_cat = sum(1 for p in profile.columns if p.semantic_type in ("categorical", "boolean"))
    assert profile.n_queries <= 3 + n_dt + min(n_cat, 12)
    assert len(profile.query_ids) == profile.n_queries
    d = profile.to_dict()
    assert d["n_queries"] == profile.n_queries and d["columns"][0]["name"] == "sys_id"


def test_histogram_rows_come_back_in_one_order(profile, duck):
    # A parallel engine (DuckDB) emits GROUP BY groups in any order; the recorded result hash must not
    # change between identical runs (DEX-001), so the statement orders its rows.
    (sql,) = {c["sql"] for c in duck.calls if c["purpose"].endswith("histograms")}
    tree = sqlglot.parse_one(sql, read="duckdb")
    assert [o.this.name for o in tree.args["order"].expressions] == ["column_name", "bin"]


def test_candidate_keys_detect_duplicates(profile):
    keys = {k["column"]: k for k in profile.candidate_keys}
    assert keys["sys_id"]["unique"] is True
    assert keys["number"]["unique"] is False and keys["number"]["duplicate_rows"] == 4


def test_quality_detects_planted_issues(duck, profile):
    issues = check_quality(duck, "itsm.incident", profile, RELS, now=NOW)
    by = {(i.code, i.column): i for i in issues}
    dup = by[("duplicate_key", "number")]
    assert dup.severity == "critical" and dup.metric["duplicate_values"] == 4 and dup.metric["rows_affected"] == 8
    assert "0.13%" in dup.message and dup.sql
    t = by[("temporal_order", "resolved_at")]
    assert t.metric["violations"] == 7 and t.metric["worst_gap_hours"] == pytest.approx(-5.0, abs=1e-6)
    assert ("temporal_order", "closed_at") not in by  # closed_at >= opened_at for every planted row
    f = by[("future_timestamp", "closed_at")]
    assert f.metric["future_rows"] == 3
    assert ("future_timestamp", "opened_at") not in by
    assert by[("high_null_rate", "u_legacy_code")].severity == "warning"
    assert by[("constant_column", "company")].severity == "info"
    cv = by[("case_variant_category", "category")]
    assert cv.metric["distinct_raw"] == 5 and cv.metric["distinct_normalized"] == 4
    assert "'network'" in cv.message
    o = by[("orphan_reference", "assignment_group")]
    assert o.metric["orphan_rows"] == 5 and o.metric["orphan_values"] == 1
    assert [i.severity for i in issues] == sorted([i.severity for i in issues], key=["critical", "warning", "info"].index)
    assert not any(i.code == "duplicate_key" and i.column == "sys_id" for i in issues)


def test_tsql_profile_and_quality_via_transpile(duck, profile):
    t = TsqlViaDuckRunSQL(duck)
    p = profile_asset(t, "itsm.incident", INCIDENT_COLUMNS)
    for call in t.calls:
        assert sqlglot.parse_one(call["sql"], read="tsql") is not None
        assert "LIMIT" not in call["sql"]
    assert any("OVER ()" in c["sql"] and "PERCENTILE_CONT" in c["sql"] for c in t.calls)
    a = {c.name: c for c in p.columns}
    b = {c.name: c for c in profile.columns}
    for name in a:
        assert (a[name].non_null, a[name].distinct, a[name].semantic_type) == (b[name].non_null, b[name].distinct,
                                                                              b[name].semantic_type)
    assert a["opened_at"].monthly_counts == b["opened_at"].monthly_counts
    assert a["noise_value"].percentiles["p50"] == pytest.approx(b["noise_value"].percentiles["p50"], rel=1e-5)
    codes = {(i.code, i.column) for i in check_quality(t, "itsm.incident", p, RELS, now=NOW)}
    assert {("duplicate_key", "number"), ("temporal_order", "resolved_at"), ("future_timestamp", "closed_at"),
            ("orphan_reference", "assignment_group"), ("case_variant_category", "category")} <= codes


def test_type_family_and_semantics():
    assert type_family("timestamp without time zone") == "datetime"
    assert type_family("character varying(40)") == "text"
    assert type_family("bit") == "boolean" and type_family("NUMERIC(10,2)") == "numeric"
    assert infer_semantic_type("customer_id", "numeric", row_count=100, non_null=100, distinct=40) == "id"
    assert infer_semantic_type("amount", "numeric", row_count=100, non_null=100, distinct=90) == "numeric"
    assert infer_semantic_type("flag", "numeric", row_count=100, non_null=100, distinct=2, min_value=0, max_value=1) == "boolean"
    assert infer_semantic_type("short_description", "text", row_count=100, non_null=100, distinct=99,
                               avg_length=80) == "text"
    assert infer_semantic_type("state", "text", row_count=10000, non_null=10000, distinct=7, avg_length=6) == "categorical"


def test_temporal_pairs():
    assert temporal_pairs(["opened_at", "resolved_at", "closed_at"]) == [
        ("opened_at", "resolved_at"), ("opened_at", "closed_at"), ("resolved_at", "closed_at")]
    assert ("contract_start", "contract_end") in temporal_pairs(["contract_start", "contract_end"])
    assert temporal_pairs(["a", "b"]) == []


@pytest.mark.integration
def test_postgres_profile_and_quality():
    if not pg_available():
        pytest.skip("local Postgres not reachable")
    import psycopg

    conn = psycopg.connect(PG_DSN)
    schema = f"aos_dq_{uuid.uuid4().hex[:8]}"
    try:
        pg_load(conn, schema)
        run = PgRunSQL(conn)
        cols = [dict(c, data_type={"VARCHAR": "text", "TIMESTAMP": "timestamp without time zone", "BIGINT": "bigint",
                                   "BOOLEAN": "boolean", "DOUBLE": "double precision"}[c["data_type"]])
                for c in INCIDENT_COLUMNS]
        p = profile_asset(run, f"{schema}.incident", cols)
        assert p.row_count == 6000
        rels = [{**RELS[0], "from_asset": f"{schema}.incident", "to_asset": f"{schema}.sys_user_group"}]
        codes = {(i.code, i.column) for i in check_quality(run, f"{schema}.incident", p, rels, now=NOW)}
        assert {("duplicate_key", "number"), ("temporal_order", "resolved_at"), ("future_timestamp", "closed_at"),
                ("orphan_reference", "assignment_group"), ("case_variant_category", "category"),
                ("high_null_rate", "u_legacy_code"), ("constant_column", "company")} <= codes
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()
