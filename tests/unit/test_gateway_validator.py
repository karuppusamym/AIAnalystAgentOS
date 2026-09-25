"""Security suite for the gateway SQL validator (fail closed)."""
from __future__ import annotations

import pytest

from analystos.contracts.policy import DataScope
from analystos.core.errors import SQLRejected
from analystos.gateway.validator import validate_sql


@pytest.fixture()
def scope() -> DataScope:
    return DataScope(
        workspace_id="ws_1",
        user_id="u_1",
        role="analyst",
        source_ids=["src_a", "src_b"],
        assets=["sn.incident", "sn.change_request", "hr.person", "other.ticket", "dup.person"],
        asset_sources={
            "sn.incident": "src_a",
            "sn.change_request": "src_a",
            "hr.person": "src_a",
            "dup.person": "src_a",
            "other.ticket": "src_b",
        },
        columns={
            "sn.incident": ["sys_id", "number", "priority", "caller_id", "caused_by", "opened_at", "assignment_group"],
            "sn.change_request": ["sys_id", "number", "type"],
            "hr.person": ["id", "name", "ssn", "dept"],
            "dup.person": ["id", "name"],
            "other.ticket": ["id", "title"],
        },
        denied_columns=["hr.person.ssn", "*.caller_id"],
        source_dialects={"src_a": "postgres", "src_b": "postgres"},
    )


@pytest.fixture()
def single_scope(scope: DataScope) -> DataScope:
    s = scope.model_copy(deep=True)
    s.source_ids = ["src_a"]
    s.assets = ["sn.incident", "sn.change_request", "hr.person"]
    s.asset_sources = {a: "src_a" for a in s.assets}
    return s


def ok(scope: DataScope, sql: str, max_rows: int = 1000):
    return validate_sql(scope, sql, max_rows=max_rows)


def rejected(scope: DataScope, sql: str) -> str:
    with pytest.raises(SQLRejected) as info:
        validate_sql(scope, sql, max_rows=1000)
    return info.value.message


# ------------------------------------------------------------------------------------ positive


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT number, priority FROM sn.incident",
        "SELECT priority, COUNT(*) AS n FROM sn.incident GROUP BY priority ORDER BY n DESC",
        "SELECT i.number FROM sn.incident i JOIN sn.change_request c ON i.caused_by = c.sys_id WHERE c.type = 'emergency'",
        "WITH p1 AS (SELECT number FROM sn.incident WHERE priority = 1) SELECT COUNT(*) FROM p1",
        "SELECT number FROM sn.incident UNION ALL SELECT number FROM sn.change_request",
        "SELECT number FROM sn.incident WHERE caused_by IN (SELECT sys_id FROM sn.change_request)",
        "SELECT name, dept FROM hr.person",
        "SELECT number, ROW_NUMBER() OVER (PARTITION BY priority ORDER BY opened_at) AS rn FROM sn.incident",
        "SELECT number FROM sn.incident -- trailing comment; DROP TABLE x",
        "SELECT number FROM sn.incident /* ; DELETE */ ;",
        "SELECT number FROM sn.incident WHERE number = 'a;DROP TABLE x'",
        "SELECT priority AS p FROM sn.incident ORDER BY p",
        "SELECT COUNT(*) FROM sn.incident",
    ],
)
def test_valid_read_queries_are_accepted(scope: DataScope, sql: str) -> None:
    v = ok(scope, sql)
    assert v.source_id == "src_a"
    assert v.dialect == "postgres"
    assert "LIMIT 1001" in v.executable_sql
    assert "--" not in v.executable_sql and "/*" not in v.executable_sql


def test_unqualified_name_resolves_when_unambiguous(scope: DataScope) -> None:
    v = ok(scope, "SELECT number FROM incident")
    assert v.referenced_assets == ["sn.incident"]
    assert '"sn"."incident"' in v.executable_sql
    assert v.referenced_columns == ["sn.incident.number"]


def test_unqualified_name_ambiguity_is_rejected(scope: DataScope) -> None:
    msg = rejected(scope, "SELECT name FROM person")
    assert "ambiguous" in msg and "dup.person" in msg and "hr.person" in msg


def test_star_is_expanded_to_known_columns(scope: DataScope) -> None:
    v = ok(scope, "SELECT * FROM sn.change_request")
    assert '"change_request"."type"' in v.executable_sql
    assert "*" not in v.executable_sql


def test_existing_limit_is_tightened_not_loosened(scope: DataScope) -> None:
    assert "LIMIT 5" in ok(scope, "SELECT number FROM sn.incident LIMIT 5").executable_sql
    assert "LIMIT 11" in ok(scope, "SELECT number FROM sn.incident LIMIT 100000", max_rows=10).executable_sql


def test_fingerprint_is_stable_and_ignores_formatting(scope: DataScope) -> None:
    a = ok(scope, "select number from sn.incident")
    b = ok(scope, "SELECT   number\nFROM sn.incident -- hi")
    c = ok(scope, "SELECT number FROM sn.incident", max_rows=10)
    assert a.fingerprint == b.fingerprint == c.fingerprint
    assert a.fingerprint != ok(scope, "SELECT priority FROM sn.incident").fingerprint


# ------------------------------------------------------------------------------------ writes & statements


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO sn.incident (number) VALUES ('x')",
        "UPDATE sn.incident SET priority = 1",
        "DELETE FROM sn.incident",
        "MERGE INTO sn.incident t USING sn.change_request s ON t.sys_id = s.sys_id WHEN MATCHED THEN DELETE",
        "DROP TABLE sn.incident",
        "CREATE TABLE sn.x AS SELECT * FROM sn.incident",
        "ALTER TABLE sn.incident ADD COLUMN x int",
        "TRUNCATE sn.incident",
        "GRANT SELECT ON sn.incident TO public",
        "COPY sn.incident TO '/tmp/out.csv'",
        "SET statement_timeout = 0",
        "BEGIN",
        "VACUUM sn.incident",
        "EXPLAIN ANALYZE SELECT number FROM sn.incident",
    ],
)
def test_non_read_statements_are_rejected(scope: DataScope, sql: str) -> None:
    msg = rejected(scope, sql)
    assert "not allowed" in msg or "Only read-only" in msg


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT number FROM sn.incident; DROP TABLE sn.incident",
        "SELECT number FROM sn.incident; SELECT 1",
        "SELECT 1;;SELECT 2",
    ],
)
def test_stacked_statements_are_rejected(scope: DataScope, sql: str) -> None:
    assert "Exactly one statement" in rejected(scope, sql)


def test_empty_statement_rejected(scope: DataScope) -> None:
    assert "empty" in rejected(scope, " ; ")


def test_data_modifying_cte_rejected(scope: DataScope) -> None:
    msg = rejected(scope, "WITH d AS (DELETE FROM sn.incident RETURNING *) SELECT * FROM d")
    assert "DELETE" in msg or "Data-modifying" in msg


def test_select_into_rejected(scope: DataScope) -> None:
    assert "INTO" in rejected(scope, "SELECT number INTO sn.copy FROM sn.incident")


@pytest.mark.parametrize("lock", ["FOR UPDATE", "FOR SHARE", "FOR NO KEY UPDATE"])
def test_row_locks_rejected(scope: DataScope, lock: str) -> None:
    assert "locking" in rejected(scope, f"SELECT number FROM sn.incident {lock}")


# ------------------------------------------------------------------------------------ scope of tables


def test_unknown_table_rejected(scope: DataScope) -> None:
    assert "Unknown table" in rejected(scope, "SELECT a FROM nowhere")


def test_table_outside_scope_rejected(scope: DataScope) -> None:
    assert "not in the authorized scope" in rejected(scope, "SELECT id FROM secret.salaries")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT relname FROM pg_catalog.pg_class",
        "SELECT table_name FROM information_schema.tables",
        "SELECT name FROM sys.objects",
        "SELECT usename FROM pg_user",
        "SELECT COUNT(*) FROM sn.incident, pg_shadow",
    ],
)
def test_system_catalogs_rejected(scope: DataScope, sql: str) -> None:
    assert "authorized scope" in rejected(scope, sql)


def test_three_part_names_rejected(scope: DataScope) -> None:
    assert "Three-part" in rejected(scope, "SELECT number FROM analytics.sn.incident")


def test_union_with_out_of_scope_table_rejected(scope: DataScope) -> None:
    assert "authorized scope" in rejected(scope, "SELECT number FROM sn.incident UNION SELECT passwd FROM pg_catalog.pg_shadow")


def test_cross_source_join_rejected(scope: DataScope) -> None:
    msg = rejected(scope, "SELECT i.number FROM sn.incident i JOIN other.ticket t ON t.title = i.number")
    assert "Cross-source" in msg


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM generate_series(1, 10)",
        "SELECT * FROM pg_ls_dir('.') AS f",
        "SELECT * FROM unnest(ARRAY[1, 2]) AS u",
        "SELECT * FROM (VALUES (1), (2)) AS v(x)",
    ],
)
def test_table_valued_functions_rejected(scope: DataScope, sql: str) -> None:
    rejected(scope, sql)


def test_cte_shadowing_real_table_does_not_escape(scope: DataScope) -> None:
    # Inside a non-recursive CTE body, `person` is the physical table (with the denied column).
    msg = rejected(scope, "WITH person AS (SELECT ssn AS name FROM hr.person) SELECT name FROM person")
    assert "hr.person.ssn" in msg
    # A CTE named like an out-of-scope table is fine because it is a CTE, not the table.
    v = ok(scope, "WITH pg_shadow AS (SELECT number FROM sn.incident) SELECT number FROM pg_shadow")
    assert v.referenced_assets == ["sn.incident"]


def test_cte_shadowing_with_star_on_denied_table(scope: DataScope) -> None:
    assert "hr.person.ssn" in rejected(scope, "WITH person AS (SELECT * FROM hr.person) SELECT name FROM person")


# ------------------------------------------------------------------------------------ denied columns


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ssn FROM hr.person",  # direct
        "SELECT * FROM hr.person",  # star
        "SELECT p.* FROM hr.person p",  # qualified star
        "SELECT ssn AS harmless FROM hr.person",  # alias
        "WITH x AS (SELECT ssn FROM hr.person) SELECT COUNT(*) FROM x",  # CTE
        "SELECT s FROM (SELECT ssn AS s FROM hr.person) q",  # subquery
        "SELECT name FROM hr.person WHERE ssn LIKE '123%'",  # where
        "SELECT a.name FROM hr.person a JOIN hr.person b ON a.ssn = b.ssn",  # join on
        "SELECT name FROM hr.person ORDER BY ssn",  # order by
        "SELECT UPPER(ssn) FROM hr.person",  # function argument
        "SELECT name FROM hr.person GROUP BY name HAVING MAX(ssn) > '0'",  # having
        "SELECT name, RANK() OVER (ORDER BY ssn) FROM hr.person",  # window
        "SELECT name FROM hr.person p WHERE EXISTS (SELECT 1 FROM hr.person q WHERE q.ssn = p.ssn)",  # correlated
        "SELECT a.name FROM hr.person a JOIN hr.person b USING (ssn)",  # using
        "SELECT COUNT(DISTINCT ssn) FROM hr.person",
    ],
)
def test_denied_column_rejected_everywhere(scope: DataScope, sql: str) -> None:
    msg = rejected(scope, sql)
    assert "hr.person.ssn" in msg and "restricted" in msg


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT caller_id FROM sn.incident",
        "SELECT * FROM sn.incident",
        "SELECT number FROM sn.incident GROUP BY number, caller_id",
    ],
)
def test_wildcard_denied_column(scope: DataScope, sql: str) -> None:
    assert "sn.incident.caller_id" in rejected(scope, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT p FROM hr.person p",
        "SELECT row_to_json(p) FROM hr.person p",
        "SELECT to_jsonb(p.*) FROM hr.person p",
        "SELECT (p).ssn FROM hr.person p",
    ],
)
def test_whole_row_and_composite_access_rejected(scope: DataScope, sql: str) -> None:
    rejected(scope, sql)


def test_order_by_expression_on_alias_named_like_denied_column(scope: DataScope) -> None:
    # In Postgres, a name inside an ORDER BY expression resolves to the *input* column.
    rejected(scope, "SELECT name AS ssn FROM hr.person ORDER BY UPPER(ssn)")


@pytest.mark.parametrize("sql", ["SELECT xmin FROM sn.incident", "SELECT nope FROM sn.incident", 'SELECT "NUMBER" FROM sn.incident'])
def test_unknown_columns_rejected(scope: DataScope, sql: str) -> None:
    msg = rejected(scope, sql)
    assert "could not be resolved" in msg or "Unknown column" in msg


# ------------------------------------------------------------------------------------ functions


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_catalog.pg_read_file('/etc/passwd')",
        "SELECT \"pg_read_file\"('/etc/passwd')",
        "SELECT number FROM sn.incident WHERE pg_sleep(5) IS NULL",
        "SELECT PG_SLEEP_FOR('5 seconds')",
        "SELECT dblink('host=x', 'select 1')",
        "SELECT dblink_exec('host=x', 'drop table y')",
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT current_setting('data_directory')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT pg_terminate_backend(1)",
        "SELECT query_to_xml('select * from hr.person', true, true, '')",
        "SELECT pg_advisory_lock(1)",
        "SELECT txid_current()",
        "SELECT nextval('seq')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT number FROM sn.incident WHERE pg_ls_dir('/') IS NOT NULL",
    ],
)
def test_function_denylist(scope: DataScope, sql: str) -> None:
    msg = rejected(scope, sql)
    assert "not allowed" in msg


def test_schema_qualified_function_calls_rejected(scope: DataScope) -> None:
    assert "Schema-qualified" in rejected(scope, "SELECT public.myfunc(number) FROM sn.incident")


# ------------------------------------------------------------------------------------ dialects


@pytest.fixture()
def tsql_scope() -> DataScope:
    return DataScope(
        workspace_id="ws_1",
        user_id="u_1",
        role="analyst",
        source_ids=["mss"],
        assets=["dbo.Orders"],
        asset_sources={"dbo.Orders": "mss"},
        columns={"dbo.Orders": ["OrderId", "Amount", "CardNumber"]},
        denied_columns=["dbo.Orders.CardNumber"],
        source_dialects={"mss": "tsql"},
    )


def test_tsql_top_wrapping(tsql_scope: DataScope) -> None:
    v = validate_sql(tsql_scope, "SELECT OrderId, Amount FROM dbo.Orders ORDER BY Amount DESC", max_rows=100)
    assert v.dialect == "tsql"
    assert "TOP 101" in v.executable_sql
    assert "LIMIT" not in v.executable_sql


def test_tsql_existing_top_is_tightened(tsql_scope: DataScope) -> None:
    v = validate_sql(tsql_scope, "SELECT TOP 5000 [OrderId] FROM [dbo].[Orders]", max_rows=100)
    assert "TOP 101" in v.executable_sql


def test_tsql_union_is_wrapped(tsql_scope: DataScope) -> None:
    v = validate_sql(tsql_scope, "SELECT OrderId FROM dbo.Orders UNION SELECT Amount FROM dbo.Orders", max_rows=10)
    assert v.executable_sql.startswith("SELECT TOP 11")


def test_tsql_denied_column_case_insensitive(tsql_scope: DataScope) -> None:
    with pytest.raises(SQLRejected, match="CardNumber"):
        validate_sql(tsql_scope, "SELECT cardnumber FROM dbo.orders", max_rows=10)


def test_tsql_dangerous_functions(tsql_scope: DataScope) -> None:
    with pytest.raises(SQLRejected):
        validate_sql(tsql_scope, "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'select 1')", max_rows=10)
    with pytest.raises(SQLRejected):
        validate_sql(tsql_scope, "EXEC xp_cmdshell 'dir'", max_rows=10)
    with pytest.raises(SQLRejected, match="Three-part|scope"):
        validate_sql(tsql_scope, "SELECT OrderId FROM otherdb.dbo.Orders", max_rows=10)


def test_mixed_dialect_scope_picks_matching_source(scope: DataScope, tsql_scope: DataScope) -> None:
    merged = scope.model_copy(deep=True)
    merged.source_ids.append("mss")
    merged.assets.append("dbo.Orders")
    merged.asset_sources["dbo.Orders"] = "mss"
    merged.columns.update(tsql_scope.columns)
    merged.source_dialects["mss"] = "tsql"
    v = validate_sql(merged, "SELECT TOP 3 OrderId FROM [dbo].[Orders]", max_rows=10)
    assert v.source_id == "mss" and v.dialect == "tsql"
    v = validate_sql(merged, "SELECT number FROM sn.incident", max_rows=10)
    assert v.source_id == "src_a" and v.dialect == "postgres"


def test_missing_column_metadata_fails_closed(single_scope: DataScope) -> None:
    single_scope.columns.pop("sn.change_request")
    assert "No column metadata" in rejected(single_scope, "SELECT number FROM sn.change_request")


def test_query_without_tables_rejected(scope: DataScope) -> None:
    assert "at least one authorized table" in rejected(scope, "SELECT 1")
