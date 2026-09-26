"""P7-02 row-level security: `{{user.attr}}` placeholders become literals, a missing attribute fails
closed (the asset is withheld with its reason), and the gateway applies the filter to every statement,
idempotently, so model-written SQL cannot read a filtered asset unfiltered."""
from __future__ import annotations

from types import SimpleNamespace

import duckdb
import pytest
from pydantic import ValidationError

from analystos.contracts.policy import DataScope, RowFilter
from analystos.core.errors import SQLRejected
from analystos.gateway.validator import validate_sql
from analystos.governance.policy import apply_row_filters
from analystos.governance.row_filters import MissingAttribute, predicate_problem, render


def _scope(dialect="duckdb"):
    return DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"],
                     assets=["sales.orders", "sales.returns"], asset_sources={"sales.orders": "s", "sales.returns": "s"},
                     columns={"sales.orders": ["amount", "region", "tier", "email"], "sales.returns": ["amount", "region"]},
                     denied_columns=["sales.orders.email"], source_dialects={"s": dialect})


def test_placeholders_become_literals_and_lists_expand_in_in():
    sql, cols = render("region IN {{user.regions}} AND tier >= {{ user.tier }}", {"regions": ["East", "O'Neil"], "tier": 2}, "postgres")
    assert sql == "region IN ('East', 'O''Neil') AND tier >= 2" and cols == ["region", "tier"]
    assert render("region IN {{user.regions}}", {"regions": []}, "postgres")[0] == "FALSE"
    assert render("region = {{user.region}}", {"region": "x') OR TRUE --"}, "postgres")[0] == "region = 'x'') OR TRUE --'"


def test_missing_attribute_raises_and_bad_predicates_are_refused_at_policy_save():
    with pytest.raises(MissingAttribute, match="regions"):
        render("region IN {{user.regions}}", {}, "postgres", filter_id="by_region")
    for bad in ("region IN (SELECT r FROM t)", "o.region = 'x'", "COUNT(*) > 1", "region = {{org.x}}", "a = 1; DROP TABLE t"):
        assert predicate_problem(bad), bad
        with pytest.raises(ValidationError):
            RowFilter(id="f", assets=["sales.orders"], predicate=bad)
    assert predicate_problem("region IN {{user.regions}} OR {{user.role}} = 'owner'") is None


def test_resolve_scope_renders_per_caller_and_withholds_on_a_missing_attribute():
    filters = [RowFilter(id="by_region", assets=["*.orders"], predicate="region IN {{user.regions}}", exempt_roles=["owner"]),
               RowFilter(id="returns", assets=["sales.returns"], predicate="region = {{user.home}}")]
    scope = _scope()
    apply_row_filters(scope, SimpleNamespace(id="u", email="u@x", attributes={"regions": ["East"]}), filters)
    assert scope.row_filters == {"sales.orders": ["region IN ('East')"]}
    assert "sales.returns" not in scope.assets and "home" in scope.withheld_assets["sales.returns"]
    with pytest.raises(SQLRejected, match="withheld"):
        validate_sql(scope, "SELECT amount FROM sales.returns", max_rows=10)
    owner = _scope()
    owner.role = "owner"
    apply_row_filters(owner, SimpleNamespace(id="u", email="u@x", attributes={"regions": ["East"], "home": "EU"}), filters)
    assert owner.row_filters == {"sales.returns": ["region = 'EU'"]}  # the owner is exempt from by_region only


def test_a_filter_over_an_unknown_column_withholds_the_asset():
    scope = _scope()
    apply_row_filters(scope, SimpleNamespace(id="u", email="u@x", attributes={"c": "x"}),
                      [RowFilter(id="f", assets=["sales.orders"], predicate="country = {{user.c}}")])
    assert "sales.orders" in scope.withheld_assets and "country" in scope.withheld_assets["sales.orders"]


@pytest.mark.parametrize("dialect", ["postgres", "duckdb", "snowflake", "tsql", "mysql", "bigquery"])
def test_gateway_wraps_every_read_and_is_idempotent(dialect):
    scope = _scope(dialect)
    scope.row_filters = {"sales.orders": [render("region IN {{user.regions}}", {"regions": ["East"]}, dialect)[0]]}
    first = validate_sql(scope, "SELECT o.region, SUM(o.amount) AS total FROM sales.orders o GROUP BY o.region", max_rows=10)
    assert first.executable_sql.count("'East'") == 1
    assert validate_sql(scope, first.executable_sql, max_rows=10).executable_sql == first.executable_sql
    # a caller cannot pass a look-alike wrapper with another value: it is wrapped again with the real filter
    forged = first.executable_sql.replace("'East'", "'West'")
    assert "'East'" in validate_sql(scope, forged, max_rows=10).executable_sql


def test_filtered_rows_only_through_joins_ctes_and_subqueries():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA sales")
    con.execute("CREATE TABLE sales.orders(amount INT, region VARCHAR, tier INT, email VARCHAR)")
    con.execute("CREATE TABLE sales.returns(amount INT, region VARCHAR)")
    con.execute("INSERT INTO sales.orders VALUES (1, 'East', 1, 'a'), (10, 'West', 1, 'b'), (100, 'East', 2, 'c')")
    con.execute("INSERT INTO sales.returns VALUES (5, 'East'), (50, 'West')")
    scope = _scope()
    scope.row_filters = {"sales.orders": ["region = 'East'"], "sales.returns": ["region = 'East'"]}
    for sql, expected in [
        ("SELECT SUM(amount) FROM sales.orders", 101),
        ("WITH x AS (SELECT amount FROM sales.orders) SELECT SUM(amount) FROM x", 101),
        ("SELECT SUM(a) FROM (SELECT amount AS a FROM orders) t", 101),
        ("SELECT SUM(r.amount) FROM sales.orders o JOIN sales.returns r ON o.region = r.region WHERE o.tier = 2", 5),
        ("SELECT SUM(amount) FROM sales.orders WHERE region = 'West'", None),
    ]:
        assert con.execute(validate_sql(scope, sql, max_rows=10).executable_sql).fetchone()[0] == expected, sql
    with pytest.raises(SQLRejected):  # the wrapper never re-exposes a denied column
        validate_sql(scope, "SELECT email FROM sales.orders", max_rows=10)
