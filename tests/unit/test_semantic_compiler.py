from copy import deepcopy
from types import SimpleNamespace

import duckdb
import pytest

from analystos.agents import sql_agent
from analystos.contracts.policy import DataScope, WorkspacePolicyDoc
from analystos.contracts.semantic import SemanticQuery
from analystos.core.errors import InvalidInput, SQLRejected
from analystos.semantic.compiler import compile_query, match_question


@pytest.fixture
def scope():
    return DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"],
                     assets=["sales.orders"], asset_sources={"sales.orders": "s"},
                     columns={"sales.orders": ["amount", "region", "paid"]}, source_dialects={"s": "duckdb"})


@pytest.fixture
def catalog():
    return {"id": "model1", "version": 1, "hash": "modelhash", "datasets": [
        {"name": "orders", "source": "SELECT amount, region, paid FROM sales.orders", "fields": [
            {"name": n, "expressions": [{"expression": n}], "dimension": {} if n == "region" else None}
            for n in ["amount", "region", "paid"]]}],
        "metrics": {"revenue": {"id": "metric1", "version": 2, "hash": "metrichash", "definition": {
            "name": "revenue", "display_name": "Paid revenue", "dataset": "orders",
            "expressions": [{"expression": "SUM(amount)"}], "dimensions": ["region"], "filters": ["paid = TRUE"]}}}}


def test_executes_approved_formula_filter_and_grouping(catalog, scope):
    query = SemanticQuery(metrics=["revenue"], dimensions=["region"])
    result = compile_query(query, catalog, scope)
    with duckdb.connect() as db:
        db.execute("CREATE SCHEMA sales")
        db.execute("CREATE TABLE sales.orders(amount DOUBLE, region VARCHAR, paid BOOLEAN)")
        db.execute("INSERT INTO sales.orders VALUES (10, 'East', true), (7, 'East', false), (3, 'West', true)")
        assert db.execute(result.sql).fetchall() == [("East", 10), ("West", 3)]
    assert result == compile_query(query, catalog, scope)
    assert result.provenance["metrics"][0]["version"] == 2


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb", "snowflake", "bigquery", "mysql"])
def test_compiles_for_supported_source_dialects(catalog, scope, dialect):
    scope.source_dialects = {"s": dialect}
    assert compile_query(SemanticQuery(metrics=["revenue"]), catalog, scope).sql


def test_matching_does_not_discard_user_constraints(catalog):
    assert match_question("What is paid revenue by region?", catalog).dimensions == ["region"]
    assert match_question("revenue last year", catalog) is None
    assert match_question("revenue by region excluding West", catalog) is None
    assert match_question("delete revenue", catalog) is None


@pytest.mark.parametrize("change", ["dataset", "dimension", "column", "join", "statement", "grain", "dialect"])
def test_unsupported_or_unsafe_definitions_refused(catalog, scope, change):
    definition = catalog["metrics"]["revenue"]["definition"]
    query = SemanticQuery(metrics=["revenue"])
    if change == "dataset":
        definition["dataset"] = "missing"
    elif change == "dimension":
        query.dimensions = ["paid"]
    elif change == "column":
        definition["expressions"][0]["expression"] = "SUM(secret)"
    elif change == "join":
        catalog["datasets"][0]["source"] += " o JOIN sales.orders b ON o.region = b.region"
    elif change == "statement":
        definition["expressions"][0]["expression"] = "SUM(amount); DELETE FROM sales.orders"
    elif change == "grain":
        definition["grain"] = "month"
    elif change == "dialect":
        definition["expressions"][0]["dialect"] = "tsql"
    with pytest.raises(InvalidInput):
        compile_query(query, catalog, scope)


def test_restricted_column_refused_in_the_compiler_and_still_by_gateway(catalog, scope):
    """P7-02: a restricted column masks its field in the compiler (refused before any SQL is built); a
    hand-written statement over it is still refused by the gateway."""
    from analystos.core.errors import PolicyDenied
    from analystos.gateway.validator import validate_sql

    scope.denied_columns = ["sales.orders.amount"]
    with pytest.raises(PolicyDenied, match="masked"):
        compile_query(SemanticQuery(metrics=["revenue"]), catalog, scope)
    with pytest.raises(SQLRejected):
        validate_sql(scope, "SELECT SUM(amount) FROM sales.orders", max_rows=10)


def test_different_metric_populations_cannot_be_combined(catalog, scope):
    other = deepcopy(catalog["metrics"]["revenue"])
    other["definition"]["name"] = "all_revenue"
    other["definition"]["filters"] = []
    catalog["metrics"]["all_revenue"] = other
    with pytest.raises(InvalidInput, match="different filters"):
        compile_query(SemanticQuery(metrics=["revenue", "all_revenue"]), catalog, scope)


def test_ask_uses_gateway_without_registry_or_model(catalog, scope, monkeypatch):
    calls = []

    def execute(sc, sql, **kw):
        calls.append((sc, sql, kw))
        return SimpleNamespace(query_id="q", columns=["revenue"], rows=[[13]], row_count=1, truncated=False)

    ctx = SimpleNamespace(semantic_catalog=catalog, scope=scope, policy=WorkspacePolicyDoc(), user=SimpleNamespace(id="u"),
                          services=SimpleNamespace(gateway=SimpleNamespace(execute=execute)), turn_id="turn1")
    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda _: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda _: None)
    monkeypatch.setattr(sql_agent, "_registry_lookup", lambda *a: pytest.fail("registry must not replace an approved metric"))
    out = sql_agent.ask(ctx, "paid revenue")
    assert out["governance"] == "governed"
    assert out["answered_by"] == "semantic"
    assert out["result"]["rows"] == [[13]]
    assert len(calls) == 1 and calls[0][2]["task_id"] == "turn1"
    assert out["semantic"]["policy_hash"]
