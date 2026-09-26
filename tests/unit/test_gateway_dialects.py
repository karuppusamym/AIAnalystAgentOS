"""Validator security suite per dialect (P4-E01, multi-dialect validation).

Each dialect in gateway/dialects.py gets the same generic suite (read-only enforcement, denied
columns through every path, one statement, DDL/DML/session statements) plus its own attack list:
the functions and syntax of that dialect that read files, call out, sleep, reveal session state,
read stages or time-travel. PostgreSQL and T-SQL also refuse unmodelled functions.
"""
from __future__ import annotations

import pytest

from analystos.contracts.policy import DataScope
from analystos.core.errors import SQLRejected
from analystos.gateway.dialects import PROFILES, SUPPORTED_DIALECTS
from analystos.gateway.validator import validate_sql

STRICT = ["postgres", "tsql", "snowflake", "bigquery", "databricks", "trino", "duckdb", "mysql"]
COLS = ["id", "region", "amount", "email", "customer_id"]


def _scope(dialect: str) -> DataScope:
    assets = {"sales.orders": COLS, "sales.customers": ["id", "name", "tier", "ssn"]}
    return DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"], assets=list(assets),
                     asset_sources={a: "s" for a in assets}, columns=assets, source_dialects={"s": dialect},
                     denied_columns=["sales.orders.email", "*.ssn"])


def ok(dialect: str, sql: str) -> str:
    return validate_sql(_scope(dialect), sql, max_rows=100).executable_sql


def rejected(dialect: str, sql: str, match: str | None = None) -> None:
    with pytest.raises(SQLRejected, match=match):
        validate_sql(_scope(dialect), sql, max_rows=100)


def test_every_strict_dialect_has_a_profile() -> None:
    assert set(STRICT) == SUPPORTED_DIALECTS
    assert all(PROFILES[d].strict for d in STRICT)


@pytest.mark.parametrize("dialect", STRICT)
@pytest.mark.parametrize("sql", [
    "SELECT o.id FROM sales.orders o, sales.customers c",
    "SELECT o.id FROM sales.orders o JOIN sales.customers c ON TRUE",
    "SELECT o.id FROM sales.orders o JOIN sales.customers c ON 1 = 1",
    "SELECT o.id FROM sales.orders o CROSS JOIN sales.customers c",
])
def test_unconditioned_join_is_rejected(dialect: str, sql: str) -> None:
    rejected(dialect, sql, "Unconditioned joins")


@pytest.mark.parametrize("hint", ["UPDLOCK", "HOLDLOCK", "NOLOCK"])
def test_tsql_table_hints_are_rejected(hint: str) -> None:
    rejected("tsql", f"SELECT o.id FROM sales.orders o WITH ({hint})", "Table hints")


def test_sqlbuild_functions_remain_available() -> None:
    ok("postgres", "SELECT DATE_TRUNC('month', o.amount) AS period FROM sales.orders o")
    ok("tsql", "SELECT DATEDIFF(DAY, 0, o.amount) AS elapsed FROM sales.orders o")


@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
def test_unknown_function_cannot_have_external_effects(dialect: str) -> None:
    rejected(dialect, "SELECT fn_send_mail(o.id) FROM sales.orders o", "not a known built-in")


# ------------------------------------------------------------------------------ generic suite
@pytest.mark.parametrize("dialect", STRICT)
def test_legitimate_queries_pass_and_are_capped(dialect: str) -> None:
    sql = ok(dialect, "SELECT o.region, SUM(o.amount) AS total FROM sales.orders o GROUP BY o.region ORDER BY total DESC")
    assert ("TOP 101" if dialect == "tsql" else "LIMIT 101") in sql
    ok(dialect, "WITH t AS (SELECT region, amount FROM sales.orders) SELECT region, COUNT(*) AS n FROM t GROUP BY region")
    ok(dialect, "SELECT c.tier, AVG(o.amount) AS a FROM sales.orders o JOIN sales.customers c ON c.id = o.customer_id "
                "GROUP BY c.tier")
    ok(dialect, "SELECT region FROM sales.orders UNION ALL SELECT tier FROM sales.customers")
    ok(dialect, "SELECT CASE WHEN amount > 10 THEN 'big' ELSE 'small' END AS size, COUNT(*) AS n FROM sales.orders "
                "GROUP BY 1")


@pytest.mark.parametrize("dialect", STRICT)
@pytest.mark.parametrize("sql", [
    "SELECT email FROM sales.orders",
    "SELECT * FROM sales.orders",
    "SELECT o.* FROM sales.orders o",
    "SELECT x FROM (SELECT email AS x FROM sales.orders) q",
    "WITH q AS (SELECT email FROM sales.orders) SELECT COUNT(*) AS n FROM q",
    "SELECT region FROM sales.orders WHERE email LIKE '%@x.com'",
    "SELECT region FROM sales.orders ORDER BY email",
    "SELECT COUNT(DISTINCT email) AS n FROM sales.orders",
    "SELECT c.ssn FROM sales.customers c",
    "SELECT region FROM sales.orders o JOIN sales.customers c ON c.ssn = o.region",
])
def test_denied_columns_are_rejected_everywhere(dialect: str, sql: str) -> None:
    rejected(dialect, sql, "restricted by policy")


@pytest.mark.parametrize("dialect", STRICT)
@pytest.mark.parametrize("sql", [
    "SELECT region FROM sales.orders; SELECT 1",
    "SELECT region FROM sales.orders; DROP TABLE sales.orders",
    "DELETE FROM sales.orders",
    "UPDATE sales.orders SET amount = 0",
    "INSERT INTO sales.orders (id) VALUES (1)",
    "DROP TABLE sales.orders",
    "CREATE TABLE sales.x AS SELECT region FROM sales.orders",
    "TRUNCATE TABLE sales.orders",
    "GRANT SELECT ON sales.orders TO PUBLIC",
    "SELECT region FROM sales.orders FOR UPDATE",
    "SELECT region FROM secret.payroll",
    "SELECT region FROM other_db.sales.orders",
    "SELECT nope FROM sales.orders",
    "",
])
def test_generic_attacks_are_rejected(dialect: str, sql: str) -> None:
    rejected(dialect, sql)


@pytest.mark.parametrize("dialect", STRICT)
def test_unknown_and_namespaced_functions_are_rejected(dialect: str) -> None:
    rejected(dialect, "SELECT my_udf(amount) AS x FROM sales.orders", "not a known built-in")
    rejected(dialect, "SELECT util.f(amount) AS x FROM sales.orders")


@pytest.mark.parametrize("dialect", STRICT)
def test_comments_never_reach_the_database(dialect: str) -> None:
    sql = ok(dialect, "SELECT region /* hidden */ FROM sales.orders -- tail")
    assert "hidden" not in sql and "tail" not in sql


# ------------------------------------------------------------------------------ per dialect
ATTACKS: dict[str, list[str]] = {
    "snowflake": [
        "SELECT * FROM @my_stage",
        "SELECT $1 FROM @my_stage/file.csv",
        "SELECT SYSTEM$WHITELIST() AS x FROM sales.orders",
        "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
        "SELECT * FROM IDENTIFIER('sales.orders')",
        "SELECT GET_PRESIGNED_URL(@s, 'a') AS u FROM sales.orders",
        "SELECT region FROM sales.orders AT(OFFSET => -60)",
        "SELECT region FROM sales.orders BEFORE(STATEMENT => 'x')",
        "SELECT region FROM sales.orders WHERE region = $var",
        "SELECT region FROM sales.orders, LATERAL FLATTEN(input => region)",
        "CALL p()", "EXECUTE IMMEDIATE 'DROP TABLE x'", "PUT file:///etc/passwd @s", "GET @s file:///tmp",
        "ALTER SESSION SET QUERY_TAG = 'x'", "COPY INTO @s FROM sales.orders", "UNDROP TABLE sales.orders",
        "USE ROLE ACCOUNTADMIN",
    ],
    "bigquery": [
        "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')",
        "SELECT EXTERNAL_QUERY('c', 'q') AS x FROM sales.orders",
        "SELECT NET.HOST(region) AS h FROM sales.orders",
        "SELECT AEAD.DECRYPT_STRING(region, region, 'a') AS x FROM sales.orders",
        "SELECT * FROM ML.PREDICT(MODEL sales.m, TABLE sales.orders)",
        "SELECT region FROM sales.orders FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()",
        "SELECT region FROM sales.orders WHERE region = @param",
        "SELECT region FROM sales.orders WHERE region = @@project_id",
        "SELECT region FROM sales.orders_*",
        "SELECT * FROM `proj.sales.orders`",
        "SELECT * FROM sales.INFORMATION_SCHEMA.JOBS",
        "DECLARE x INT64", "EXECUTE IMMEDIATE 'SELECT 1'", "CALL p()",
        "EXPORT DATA OPTIONS(uri='gs://x/*') AS SELECT region FROM sales.orders",
        "LOAD DATA INTO sales.orders FROM FILES(uris=['gs://x'])",
    ],
    "databricks": [
        "SELECT * FROM parquet.`/tmp/x`",
        "SELECT * FROM csv.`s3://bucket/key`",
        "SELECT * FROM delta.`/mnt/table`",
        "SELECT * FROM `/path/file`",
        "SELECT * FROM read_files('/x')",
        "SELECT reflect('java.lang.Runtime', 'getRuntime') AS r FROM sales.orders",
        "SELECT java_method('java.lang.System', 'getenv') AS r FROM sales.orders",
        "SELECT ai_query('model', region) AS a FROM sales.orders",
        "SELECT http_request('GET', 'http://x') AS r FROM sales.orders",
        "SELECT secret('scope', 'key') AS s FROM sales.orders",
        "SELECT input_file_name() AS f FROM sales.orders",
        "SELECT region FROM sales.orders VERSION AS OF 1",
        "SELECT region FROM sales.orders TIMESTAMP AS OF '2020-01-01'",
        "SELECT ${var} AS v FROM sales.orders",
        "SELECT region FROM sales.orders WHERE region = :p",
        "SET spark.sql.x = 1", "ADD JAR /x.jar", "CACHE TABLE sales.orders", "OPTIMIZE sales.orders",
        "VACUUM sales.orders", "MSCK REPAIR TABLE sales.orders", "REFRESH TABLE sales.orders",
    ],
    "trino": [
        "SELECT * FROM TABLE(system.query(query => 'SELECT 1'))",
        "SELECT * FROM system.runtime.queries",
        "SELECT \"$path\" FROM sales.orders",
        "SELECT * FROM \"sales\".\"orders$partitions\"",
        "SELECT region FROM sales.orders FOR TIMESTAMP AS OF TIMESTAMP '2020-01-01 00:00:00'",
        "SELECT region FROM sales.orders FOR VERSION AS OF 3",
        "SELECT region FROM sales.orders WHERE region = ?",
        "SELECT sequence(1, 100000000) AS s FROM sales.orders",
        "CALL system.flush_metadata_cache()", "SET SESSION query_max_run_time = '1h'", "EXPLAIN ANALYZE SELECT 1",
        "PREPARE p FROM SELECT 1", "EXECUTE p", "USE sales", "SHOW TABLES",
        "REFRESH MATERIALIZED VIEW sales.v",
    ],
    "duckdb": [
        "SELECT * FROM 'x.csv'",
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM \"data.parquet\"",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM sniff_csv('x')",
        "SELECT * FROM query('SELECT 1')",
        "SELECT * FROM glob('*')",
        "SELECT getenv('HOME') AS h FROM sales.orders",
        "SELECT current_setting('threads') AS t FROM sales.orders",
        "SELECT getvariable('x') AS v FROM sales.orders",
        "SELECT COLUMNS('.*') FROM sales.orders",
        "SELECT COLUMNS('e.*') FROM sales.orders",
        "SELECT which_secret('s3://x', 's3') AS w FROM sales.orders",
        "SELECT * FROM duckdb_settings()",
        "SELECT json_execute_serialized_sql('x') AS j FROM sales.orders",
        "SELECT region FROM sales.orders WHERE region = $1",
        "SELECT region FROM sales.orders WHERE region = ?",
        "ATTACH 'x.db'", "DETACH x", "INSTALL httpfs", "LOAD httpfs", "SET threads = 1", "PRAGMA database_list",
        "COPY sales.orders TO 'x.csv'", "SUMMARIZE sales.orders", "DESCRIBE sales.orders", "CALL pragma_version()",
        "USE x", "SET VARIABLE x = 1",
    ],
    "mysql": [
        "SELECT LOAD_FILE('/etc/passwd') AS f FROM sales.orders",
        "SELECT SLEEP(5) AS s FROM sales.orders",
        "SELECT BENCHMARK(1000000, MD5('a')) AS b FROM sales.orders",
        "SELECT GET_LOCK('a', 10) AS l FROM sales.orders",
        "SELECT sys_exec('id') AS x FROM sales.orders",
        "SELECT @@secure_file_priv AS p FROM sales.orders",
        "SELECT region FROM sales.orders WHERE region = @v",
        "SELECT @a := 1 AS a FROM sales.orders",
        "SELECT region FROM sales.orders INTO OUTFILE '/tmp/x'",
        "SELECT region INTO DUMPFILE '/tmp/x' FROM sales.orders",
        "SELECT region FROM sales.orders LOCK IN SHARE MODE",
        "SELECT region FROM sales.orders WHERE region = ?",
        "SET @a = 1", "LOAD DATA INFILE 'x' INTO TABLE sales.orders", "HANDLER sales.orders OPEN", "DO SLEEP(1)",
        "CALL p()", "TABLE sales.orders",
    ],
}


@pytest.mark.parametrize(("dialect", "sql"), [(d, q) for d, qs in ATTACKS.items() for q in qs])
def test_dialect_specific_attacks_are_rejected(dialect: str, sql: str) -> None:
    rejected(dialect, sql)


def test_mysql_executable_comments_are_dropped() -> None:
    sql = ok("mysql", "SELECT region FROM sales.orders /*!50000 UNION SELECT LOAD_FILE('/etc/passwd') */")
    assert "LOAD_FILE" not in sql.upper() and "UNION" not in sql.upper()


def test_snowflake_folds_unquoted_identifiers_upper() -> None:
    sql = ok("snowflake", "SELECT region, SUM(amount) AS total FROM sales.orders GROUP BY region")
    assert '"SALES"."ORDERS"' in sql and '"REGION"' in sql
    rejected("snowflake", "SELECT email FROM sales.orders", "restricted by policy")
    rejected("snowflake", 'SELECT "EMAIL" FROM sales.orders', "restricted by policy")


def test_dialect_quoting_in_output() -> None:
    assert "`sales`.`orders`" in ok("bigquery", "SELECT region FROM sales.orders")
    assert "`sales`.`orders`" in ok("databricks", "SELECT region FROM sales.orders")
    assert "`sales`.`orders`" in ok("mysql", "SELECT region FROM sales.orders")
    assert '"sales"."orders"' in ok("trino", "SELECT region FROM sales.orders")


def test_postgres_function_boundary() -> None:
    ok("postgres", "SELECT DATE_TRUNC('month', amount) AS x FROM sales.orders")
    rejected("postgres", "SELECT my_udf(amount) AS x FROM sales.orders", "not a known built-in")
    rejected("postgres", "SELECT region FROM sales.orders WHERE region = $1", "Parameters")
    rejected("postgres", "SELECT pg_read_file('/etc/passwd') AS f FROM sales.orders", "not allowed")
    rejected("postgres", "SELECT email FROM sales.orders", "restricted by policy")
