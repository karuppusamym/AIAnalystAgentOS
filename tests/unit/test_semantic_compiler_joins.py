"""P7-02 compiler: the complete SemanticQuery IR (filters, time, order), joins over validated
relationships, fan-out refusal naming the edge and the fix, declared pre-aggregation, row filters and
column masks applied in the compiler, and identical SQL per model version. Executed on DuckDB."""
from __future__ import annotations

from copy import deepcopy

import duckdb
import pytest

from analystos.contracts.policy import DataScope
from analystos.contracts.semantic import SemanticQuery
from analystos.core.errors import InvalidInput, PolicyDenied
from analystos.gateway.validator import validate_sql
from analystos.governance.row_filters import render
from analystos.semantic.compiler import compile_query, match_question, planner_catalog

COLUMNS = {
    "sales.orders": ["order_id", "customer_id", "amount", "paid", "order_date", "email"],
    "sales.customers": ["customer_id", "segment", "country"],
    "sales.order_lines": ["order_id", "product", "qty"],
}


def _field(name, *, dim=None, expr=None):
    return {"name": name, "expressions": [{"dialect": "ANSI_SQL", "expression": expr or name}], "dimension": dim}


def _rel(name, frm, to, fc, tc, card="many_to_one", validated=True):
    r = {"name": name, "from": frm, "to": to, "from_columns": fc, "to_columns": tc, "cardinality": card}
    if validated:
        r |= {"validated_at": "2026-09-26T10:00:00+00:00", "validated_by": "approver"}
    return r


@pytest.fixture
def scope():
    return DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"], assets=sorted(COLUMNS),
                     asset_sources={a: "s" for a in COLUMNS}, columns=deepcopy(COLUMNS), source_dialects={"s": "duckdb"})


@pytest.fixture
def catalog():
    metric = {"name": "revenue", "display_name": "Paid revenue", "dataset": "orders",
              "expressions": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}], "filters": ["paid = TRUE"],
              "dimensions": ["order_date", "customers.segment", "order_lines.product", "customer_id"],
              "ai_context": {"synonyms": ["sales takings"]}}
    buyers = {"name": "buyers", "dataset": "orders", "expressions": [{"expression": "COUNT(DISTINCT customer_id)"}],
              "filters": ["paid = TRUE"], "dimensions": ["order_lines.product"]}
    return {"id": "m1", "version": 3, "hash": "h", "synonyms": {"turnover": "revenue"},
            "datasets": [
                {"name": "orders", "source": "sales.orders", "fields": [
                    _field("order_id"), _field("customer_id", dim={"is_time": False}), _field("amount"), _field("paid"),
                    _field("order_date", dim={"is_time": True}), _field("email", dim={"is_time": False})]},
                {"name": "customers", "source": "SELECT customer_id, segment, country FROM sales.customers", "fields": [
                    _field("customer_id"), _field("segment", dim={"is_time": False}), _field("country", dim={"is_time": False})]},
                {"name": "order_lines", "source": "sales.order_lines", "fields": [
                    _field("order_id"), _field("product", dim={"is_time": False}), _field("qty")]}],
            "relationships": [_rel("orders_customer", "orders", "customers", ["customer_id"], ["customer_id"]),
                              _rel("lines_order", "order_lines", "orders", ["order_id"], ["order_id"])],
            "metrics": {"revenue": {"id": "r1", "version": 2, "hash": "rh", "definition": metric},
                        "buyers": {"id": "b1", "version": 1, "hash": "bh", "definition": buyers}}}


@pytest.fixture
def db():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA sales")
    con.execute("CREATE TABLE sales.orders(order_id INT, customer_id INT, amount DOUBLE, paid BOOLEAN, order_date DATE, email VARCHAR)")
    con.execute("CREATE TABLE sales.customers(customer_id INT, segment VARCHAR, country VARCHAR)")
    con.execute("CREATE TABLE sales.order_lines(order_id INT, product VARCHAR, qty INT)")
    con.execute("""INSERT INTO sales.orders VALUES (1, 10, 100, true, DATE '2025-01-05', 'a@x'), (2, 10, 50, true, DATE '2025-02-07', 'a@x'),
                   (3, 11, 30, false, DATE '2025-02-09', 'b@x'), (4, 12, 20, true, DATE '2025-03-01', 'c@x')""")
    con.execute("INSERT INTO sales.customers VALUES (10, 'Enterprise', 'DE'), (11, 'SMB', 'FR'), (12, 'SMB', 'DE')")
    con.execute("INSERT INTO sales.order_lines VALUES (1, 'A', 1), (1, 'B', 2), (1, 'B', 1), (2, 'A', 1), (4, 'C', 5)")
    yield con
    con.close()


def rows(db, sql):
    return sorted(db.execute(sql).fetchall(), key=repr)


def test_many_to_one_dimension_joins_without_fanout(catalog, scope, db):
    out = compile_query(SemanticQuery(metrics=["revenue"], dimensions=["customers.segment"]), catalog, scope)
    assert rows(db, out.sql) == [("Enterprise", 150.0), ("SMB", 20.0)]
    assert out.provenance["joins"] == [{"relationship": "orders_customer", "from": "orders", "to": "customers",
                                        "cardinality": "many_to_one", "pre_aggregated": False}]
    assert out.provenance["semantic_model_version"] == 3 and out.provenance["compiler_version"] == "semantic.v2"


def test_additive_measure_across_one_to_many_is_refused_naming_the_edge_and_fix(catalog, scope):
    with pytest.raises(InvalidInput) as refused:
        compile_query(SemanticQuery(metrics=["revenue"], dimensions=["order_lines.product"]), catalog, scope)
    assert "lines_order" in refused.value.message and "pre_aggregations" in refused.value.message
    assert refused.value.details["edge"] == "lines_order" and refused.value.details["cardinality"] == "one_to_many"
    assert refused.value.details["fix"] == ["pre_aggregate", "distinct_measure"]


def test_many_to_many_edge_is_refused_even_forwards(catalog, scope):
    catalog["relationships"][0]["cardinality"] = "many_to_many"
    with pytest.raises(InvalidInput, match="orders_customer.*many_to_many"):
        compile_query(SemanticQuery(metrics=["revenue"], dimensions=["customers.segment"]), catalog, scope)


def test_distinct_measure_crosses_the_fanout_edge(catalog, scope, db):
    out = compile_query(SemanticQuery(metrics=["buyers"], dimensions=["order_lines.product"]), catalog, scope)
    assert rows(db, out.sql) == [("A", 1), ("B", 1), ("C", 1)]


def test_declared_pre_aggregation_counts_each_order_once_per_product(catalog, scope, db):
    catalog["metrics"]["revenue"]["definition"]["pre_aggregations"] = ["lines_order"]
    out = compile_query(SemanticQuery(metrics=["revenue"], dimensions=["order_lines.product"]), catalog, scope)
    # order 1 (100) has products A, B, B: counted once for A and once for B, not three times
    assert rows(db, out.sql) == [("A", 150.0), ("B", 100.0), ("C", 20.0)]
    assert out.provenance["joins"][0]["pre_aggregated"] is True
    only_b = compile_query(SemanticQuery(metrics=["revenue"], filters=[{"field": "order_lines.product", "op": "=", "value": "B"}]),
                           catalog, scope)
    assert rows(db, only_b.sql) == [(100.0,)]


def test_unvalidated_relationship_is_refused(catalog, scope):
    catalog["relationships"][0] = _rel("orders_customer", "orders", "customers", ["customer_id"], ["customer_id"], validated=False)
    with pytest.raises(InvalidInput, match="no validated cardinality") as refused:
        compile_query(SemanticQuery(metrics=["revenue"], dimensions=["customers.segment"]), catalog, scope)
    assert refused.value.details["edge"] == "orders_customer"


def test_two_equal_join_paths_are_refused_not_guessed(catalog, scope):
    catalog["relationships"].append(_rel("orders_customer_2", "orders", "customers", ["order_id"], ["customer_id"]))
    with pytest.raises(InvalidInput, match="More than one join path"):
        compile_query(SemanticQuery(metrics=["revenue"], dimensions=["customers.segment"]), catalog, scope)


def test_filters_time_grain_window_and_order(catalog, scope, db):
    query = SemanticQuery(metrics=["revenue"], filters=[{"field": "customers.segment", "op": "in", "value": ["Enterprise", "SMB"]}],
                          time={"dimension": "order_date", "grain": "month", "start": "2025-01-01", "end": "2025-03-01"},
                          order=[{"field": "revenue", "direction": "desc"}])
    out = compile_query(query, catalog, scope)
    got = db.execute(out.sql).fetchall()
    assert [(str(m)[:7], v) for m, v in got] == [("2025-01", 100.0), ("2025-02", 50.0)]
    assert compile_query(query, catalog, scope).sql == out.sql  # same question, same SQL


def test_same_query_same_sql_per_model_version_and_new_version_changes_provenance(catalog, scope):
    q = SemanticQuery(metrics=["revenue"], dimensions=["customers.segment"])
    first, second = compile_query(q, catalog, scope), compile_query(q, deepcopy(catalog), scope)
    assert first.sql == second.sql and first.provenance["sql_hash"] == second.provenance["sql_hash"]
    catalog["version"] = 4
    assert compile_query(q, catalog, scope).provenance["model_version"] == 4


@pytest.mark.parametrize("bad", [
    {"dimensions": ["country"]},  # not approved for the metric
    {"dimensions": ["amount"]},  # not a dimension
    {"order": [{"field": "segment"}]},  # not requested
    {"time": {"dimension": "customer_id", "grain": "month"}},  # not a time dimension
    {"filters": [{"field": "nope", "op": "=", "value": 1}]},
])
def test_ir_references_are_checked_against_the_approved_model(catalog, scope, bad):
    with pytest.raises(InvalidInput):
        compile_query(SemanticQuery(metrics=["revenue"], **bad), catalog, scope)


def test_filter_values_are_literals_not_sql(catalog, scope, db):
    out = compile_query(SemanticQuery(metrics=["revenue"], filters=[
        {"field": "customers.segment", "op": "=", "value": "x' OR 1=1 --"}]), catalog, scope)
    assert rows(db, out.sql) == [(None,)]


def test_row_filters_are_applied_in_the_compiler_and_kept_by_the_gateway(catalog, scope, db):
    predicate, _ = render("country IN {{user.countries}}", {"countries": ["DE"]}, "duckdb")
    scope.row_filters = {"sales.customers": [predicate]}
    out = compile_query(SemanticQuery(metrics=["revenue"], dimensions=["customers.segment"]), catalog, scope)
    assert "'DE'" in out.sql and out.provenance["row_filtered_assets"] == ["sales.customers"]
    assert validate_sql(scope, out.sql, max_rows=500).executable_sql.count("'DE'") == out.sql.count("'DE'")
    assert rows(db, out.sql) == [("Enterprise", 150.0), ("SMB", 20.0)]
    predicate, _ = render("customer_id <> {{user.hidden}}", {"hidden": 12}, "duckdb")
    scope.row_filters = {"sales.orders": [predicate]}
    out = compile_query(SemanticQuery(metrics=["revenue"]), catalog, scope)
    assert rows(db, out.sql) == [(150.0,)]


def test_withheld_asset_fails_closed_with_its_reason(catalog, scope):
    scope.assets.remove("sales.customers")
    scope.withheld_assets = {"sales.customers": "row filter by_country needs the user attribute 'countries', which is not set"}
    with pytest.raises(PolicyDenied, match="countries"):
        compile_query(SemanticQuery(metrics=["revenue"], dimensions=["customers.segment"]), catalog, scope)


def test_masked_fields_are_absent_from_the_planner_catalog_and_refused(catalog, scope):
    catalog["metrics"]["revenue"]["definition"]["dimensions"].append("email")
    scope.denied_columns = ["sales.orders.email", "sales.customers.segment"]
    seen = planner_catalog(catalog, scope)
    orders = next(d for d in seen["datasets"] if d["name"] == "orders")
    assert "email" not in {f["name"] for f in orders["fields"]}
    assert "segment" not in {f["name"] for d in seen["datasets"] for f in d["fields"]}
    assert "email" not in seen["metrics"]["revenue"]["definition"]["dimensions"]
    with pytest.raises(PolicyDenied, match="masked"):
        compile_query(SemanticQuery(metrics=["revenue"], dimensions=["email"]), catalog, scope)
    with pytest.raises(PolicyDenied, match="masked"):
        compile_query(SemanticQuery(metrics=["revenue"], dimensions=["customers.segment"]), catalog, scope)


def test_a_metric_over_a_masked_column_is_not_offered(catalog, scope):
    scope.denied_columns = ["*.amount"]
    assert "revenue" not in planner_catalog(catalog, scope)["metrics"]


def test_rules_rung_matches_names_synonyms_and_time_grains(catalog):
    assert match_question("turnover", catalog) == SemanticQuery(metrics=["revenue"])
    assert match_question("Sales takings?", catalog) == SemanticQuery(metrics=["revenue"])
    assert match_question("monthly paid revenue", catalog).time.grain == "month"
    assert match_question("revenue per quarter", catalog).time.dimension == "order_date"
    assert match_question("revenue by segment", catalog).dimensions == ["customers.segment"]
    assert match_question("revenue per month in Germany", catalog) is None


def test_multi_fact_queries_are_refused(catalog, scope):
    catalog["metrics"]["units"] = {"id": "u", "version": 1, "hash": "uh", "definition": {
        "name": "units", "dataset": "order_lines", "expressions": [{"expression": "SUM(qty)"}], "filters": ["paid = TRUE"]}}
    with pytest.raises(InvalidInput, match="different datasets"):
        compile_query(SemanticQuery(metrics=["revenue", "units"]), catalog, scope)
