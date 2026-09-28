"""Relationship evidence (Stream A): composite primary keys are one declared key, query-history joins corroborate
a join, the table-base name rule finds `<entity>_id` targets behind modelling prefixes."""
from __future__ import annotations

import duckdb
import pytest
from tests.skills_fixtures import DuckRunSQL

from analystos.skills.relationships import (
    RelationshipCandidate,
    assess_relationship,
    discover_relationships,
    facts_for,
    join_key,
    table_base,
    with_assessment,
)

LINES = {"asset": "s.order_lines", "columns": [{"name": "order_id", "data_type": "INTEGER", "is_key": True, "nullable": False},
                                               {"name": "line_no", "data_type": "INTEGER", "is_key": True, "nullable": False},
                                               {"name": "qty", "data_type": "INTEGER"}]}
ORDERS = {"asset": "s.orders", "columns": [{"name": "id", "data_type": "INTEGER", "is_key": True, "nullable": False}]}


def _cand(**ev):
    return RelationshipCandidate(from_asset="s.order_lines", from_column="order_id", to_asset="s.orders", to_column="id",
                                 cardinality="many_to_one", confidence=0.9,
                                 evidence={"source": "name_heuristic", "fk_rows": 40, "fk_distinct": 10,
                                           "target_rows": 10, "target_distinct": 10, "containment": 1.0, **ev})


def test_a_composite_key_part_is_not_a_key_on_its_own():
    facts = facts_for(_cand(), [LINES, ORDERS])
    assert facts.source.declared_keys == (frozenset({"order_id", "line_no"}),)
    a = assess_relationship(facts)
    # order_id repeats (it is half of the key): that is not a broken declared key
    assert "DECLARED_KEY_NOT_UNIQUE" not in a.warnings
    assert a.cardinality == "many_to_one" and a.approvable


def test_query_history_joins_corroborate_a_name_only_match():
    generic_from = {"asset": "s.a", "columns": [{"name": "code", "data_type": "TEXT"}]}
    generic_to = {"asset": "s.b", "columns": [{"name": "code", "data_type": "TEXT"}]}
    c = RelationshipCandidate(from_asset="s.a", from_column="code", to_asset="s.b", to_column="code",
                              cardinality="many_to_many", confidence=0.5,
                              evidence={"fk_rows": 10, "fk_distinct": 5, "target_rows": 10, "target_distinct": 5,
                                        "containment": 1.0})
    assert not with_assessment(c.model_copy(), [generic_from, generic_to]).assessment["approvable"]
    observed = {join_key("s.b", "code", "s.a", "CODE"): 7}  # undirected, case-insensitive
    seen = with_assessment(c.model_copy(), [generic_from, generic_to], observed=observed)
    assert seen.assessment["approvable"] and seen.evidence["observed_joins"] == 7
    assert any(e["name"] == "OBSERVED_QUERY_JOIN" for e in seen.assessment["evidence_classes"])


@pytest.mark.parametrize(("name", "base"), [("dim_customers", "customer"), ("stg_order_categories", "order_category"),
                                            ("fact_sales", "sale"), ("raw_src_address", "address"), ("status", "status")])
def test_table_base(name, base):
    assert table_base(name) == base


def test_table_base_heuristic_finds_prefixed_dimension():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA m")
    con.execute("CREATE TABLE m.dim_customers AS SELECT i AS customer_key, 'c' || i AS name FROM range(1, 11) t(i)")
    con.execute("CREATE TABLE m.fact_orders AS SELECT i AS id, 1 + (i % 10) AS customer_id FROM range(1, 51) t(i)")
    dims = {"asset": "m.dim_customers", "columns": [{"name": "customer_key", "data_type": "BIGINT", "is_key": True},
                                                    {"name": "name", "data_type": "VARCHAR"}]}
    facts = {"asset": "m.fact_orders", "columns": [{"name": "id", "data_type": "BIGINT", "is_key": True},
                                                   {"name": "customer_id", "data_type": "BIGINT"}]}
    (rel,) = [r for r in discover_relationships(DuckRunSQL(con), [dims, facts]) if r.from_column == "customer_id"]
    assert (rel.to_asset, rel.to_column, rel.cardinality) == ("m.dim_customers", "customer_key", "many_to_one")
    assert rel.evidence["containment"] == 1.0 and rel.evidence["source"] == "name_heuristic"
