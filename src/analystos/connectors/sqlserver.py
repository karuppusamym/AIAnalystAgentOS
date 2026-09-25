"""SQL Server connector (pushdown, dialect ``tsql``) over ``mssql+pymssql``.

pymssql is an optional extra (``analystos[sqlserver]``); it is imported only when a connection is
opened. SQL Server has no per-transaction read-only switch, so the configured identity MUST be
a least-privilege reader (db_datareader or explicit SELECT grants); the gateway's validator is the
second line of defence and the query timeout is enforced by the driver.

URL, include/exclude, max_tables and (for an explicitly staged source) extraction come from the
generic SQL connector; discovery keeps the INFORMATION_SCHEMA/sys queries below (one round trip
each, row counts from sys.dm_db_partition_stats when VIEW DATABASE STATE is granted).
"""
from __future__ import annotations

from typing import Any

from analystos.connectors.base import ConnectionTest, DiscoveredAsset
from analystos.connectors.generic_sql import GenericSQLConnector, matches_filters
from analystos.connectors.sql_metadata import build_assets
from analystos.core.errors import UpstreamUnavailable

SYSTEM_SCHEMAS = {"sys", "INFORMATION_SCHEMA", "information_schema", "guest"}

TABLES_SQL = """
SELECT t.TABLE_SCHEMA AS [schema], t.TABLE_NAME AS [table], t.TABLE_TYPE AS table_type
FROM INFORMATION_SCHEMA.TABLES t
WHERE t.TABLE_SCHEMA IN ({schemas}) AND t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME
"""

COLUMNS_SQL = """
SELECT c.TABLE_SCHEMA AS [schema], c.TABLE_NAME AS [table], c.COLUMN_NAME AS [column],
       c.DATA_TYPE AS data_type, c.IS_NULLABLE AS is_nullable, c.ORDINAL_POSITION AS ordinal
FROM INFORMATION_SCHEMA.COLUMNS c
WHERE c.TABLE_SCHEMA IN ({schemas})
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
"""

PRIMARY_KEYS_SQL = """
SELECT k.TABLE_SCHEMA AS [schema], k.TABLE_NAME AS [table], k.COLUMN_NAME AS [column]
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
  ON k.CONSTRAINT_NAME = tc.CONSTRAINT_NAME AND k.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA IN ({schemas})
"""

FOREIGN_KEYS_SQL = """
SELECT k.TABLE_SCHEMA AS [schema], k.TABLE_NAME AS [table], k.COLUMN_NAME AS [column],
       u.TABLE_SCHEMA AS ref_schema, u.TABLE_NAME AS ref_table, u.COLUMN_NAME AS ref_column
FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
  ON k.CONSTRAINT_NAME = rc.CONSTRAINT_NAME AND k.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE u
  ON u.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME AND u.CONSTRAINT_SCHEMA = rc.UNIQUE_CONSTRAINT_SCHEMA
 AND u.ORDINAL_POSITION = k.ORDINAL_POSITION
WHERE k.TABLE_SCHEMA IN ({schemas})
"""

# Needs VIEW DATABASE STATE; discovery tolerates its absence (row counts become unknown).
ROW_COUNTS_SQL = """
SELECT s.name AS [schema], o.name AS [table], SUM(p.row_count) AS row_count
FROM sys.dm_db_partition_stats p
JOIN sys.objects o ON o.object_id = p.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE p.index_id IN (0, 1) AND s.name IN ({schemas})
GROUP BY s.name, o.name
"""


def _quote_literal(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


def schema_list_sql(schemas: list[str]) -> str:
    return ", ".join(_quote_literal(s) for s in schemas)


def catalog_queries(schemas: list[str]) -> dict[str, str]:
    """The discovery statements with the schema filter inlined as escaped N'...' literals."""
    s = schema_list_sql(schemas)
    return {
        "tables": TABLES_SQL.format(schemas=s),
        "columns": COLUMNS_SQL.format(schemas=s),
        "primary_keys": PRIMARY_KEYS_SQL.format(schemas=s),
        "foreign_keys": FOREIGN_KEYS_SQL.format(schemas=s),
        "row_counts": ROW_COUNTS_SQL.format(schemas=s),
    }


def pymssql_available() -> bool:
    try:
        import pymssql  # noqa: F401
    except ImportError:
        return False
    return True


class SQLServerConnector(GenericSQLConnector):
    def __init__(self, config: dict[str, Any], secret_ref: str | None = None, *, password: str | None = None,
                 **kwargs: Any) -> None:
        config = dict(config or {})
        if not config.get("schemas"):
            config["schemas"] = ["dbo"]
        if config.get("login_timeout") and not config.get("connect_timeout"):
            config["connect_timeout"] = config["login_timeout"]
        super().__init__("sqlserver", config, secret_ref, password=password, **kwargs)
        self.host = str(config.get("host") or "")
        self.port = int(config.get("port") or 1433)
        self.database = str(config.get("database") or "")
        self.username = str(config.get("username") or "")
        self.login_timeout = self.connect_timeout

    def _require_driver(self) -> None:
        if not pymssql_available():
            raise UpstreamUnavailable(
                "SQL Server support needs the optional 'pymssql' driver: pip install 'analystos[sqlserver]'"
            )

    def test(self) -> ConnectionTest:
        if not pymssql_available():
            return ConnectionTest(
                ok=False,
                message="Could not connect: the optional 'pymssql' driver is missing (pip install 'analystos[sqlserver]')",
            )
        return super().test()

    def discover(self) -> list[DiscoveredAsset]:
        from sqlalchemy import text

        self._require_driver()
        queries = catalog_queries(list(self.schemas or ["dbo"]))
        engine = self._get_engine()
        try:
            with engine.connect() as conn:
                rows = {k: [dict(r._mapping) for r in conn.execute(text(q))] for k, q in queries.items() if k != "row_counts"}
                try:
                    counts = [dict(r._mapping) for r in conn.execute(text(queries["row_counts"]))]
                except Exception:  # noqa: BLE001 - VIEW DATABASE STATE not granted
                    counts = []
        except UpstreamUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UpstreamUnavailable(f"SQL Server discovery failed: {exc.__class__.__name__}") from None
        tables = [t for t in rows["tables"] if matches_filters(t["schema"], t["table"], self.include, self.exclude)]
        self.truncated = len(tables) > self.max_tables
        assets = build_assets(tables[: self.max_tables], rows["columns"], rows["primary_keys"], rows["foreign_keys"], counts)
        return self._staged_names(assets) if self.execution_mode == "staged" else assets
