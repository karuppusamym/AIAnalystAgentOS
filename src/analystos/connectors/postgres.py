"""PostgreSQL connector (pushdown).

The gateway queries the source in place through ``sqlalchemy_url()``, which must name a
least-privilege, read-only identity; the gateway additionally wraps every query in a READ ONLY
transaction with a statement timeout. Discovery reads information_schema for tables/columns,
pg_catalog constraints for declared primary/foreign keys and pg_class.reltuples for row counts.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, Literal

import pyarrow as pa
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

from analystos.connectors.base import ConnectionTest, DiscoveredAsset
from analystos.connectors.secrets import resolve_secret
from analystos.connectors.sql_metadata import build_assets
from analystos.core.errors import InvalidInput, UpstreamUnavailable

SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}

TABLES_SQL = """
SELECT table_schema AS schema, table_name AS "table", table_type,
       obj_description(format('%I.%I', table_schema, table_name)::regclass, 'pg_class') AS description
FROM information_schema.tables
WHERE table_schema = ANY(:schemas) AND table_type IN ('BASE TABLE', 'VIEW')
ORDER BY table_schema, table_name
"""

COLUMNS_SQL = """
SELECT c.table_schema AS schema, c.table_name AS "table", c.column_name AS "column",
       c.data_type, c.is_nullable, c.ordinal_position AS ordinal,
       col_description(format('%I.%I', c.table_schema, c.table_name)::regclass, c.ordinal_position) AS description
FROM information_schema.columns c
WHERE c.table_schema = ANY(:schemas)
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

PRIMARY_KEYS_SQL = """
SELECT n.nspname AS schema, t.relname AS "table", a.attname AS "column"
FROM pg_constraint con
JOIN pg_class t ON t.oid = con.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN LATERAL unnest(con.conkey) AS k(attnum) ON true
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE con.contype = 'p' AND n.nspname = ANY(:schemas)
"""

FOREIGN_KEYS_SQL = """
SELECT n.nspname AS schema, t.relname AS "table", a.attname AS "column",
       rn.nspname AS ref_schema, rt.relname AS ref_table, ra.attname AS ref_column
FROM pg_constraint con
JOIN pg_class t ON t.oid = con.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_class rt ON rt.oid = con.confrelid
JOIN pg_namespace rn ON rn.oid = rt.relnamespace
JOIN LATERAL unnest(con.conkey, con.confkey) AS k(attnum, refnum) ON true
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
JOIN pg_attribute ra ON ra.attrelid = rt.oid AND ra.attnum = k.refnum
WHERE con.contype = 'f' AND n.nspname = ANY(:schemas)
"""

ROW_COUNTS_SQL = """
SELECT n.nspname AS schema, c.relname AS "table",
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS row_count
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm') AND n.nspname = ANY(:schemas)
"""


class PostgresConnector:
    kind = "postgres"
    execution_mode: Literal["pushdown", "staged"] = "pushdown"
    dialect = "postgres"

    def __init__(self, config: dict[str, Any], secret_ref: str | None = None, *, password: str | None = None) -> None:
        self.host = str(config.get("host") or "")
        self.port = int(config.get("port") or 5432)
        self.database = str(config.get("database") or "")
        self.username = str(config.get("username") or "")
        schemas = config.get("schemas") or ["public"]
        if isinstance(schemas, str):
            schemas = [schemas]
        self.schemas = [s for s in schemas if s not in SYSTEM_SCHEMAS]
        self.sslmode = config.get("sslmode")
        self.connect_timeout = int(config.get("connect_timeout", 10))
        if not (self.host and self.database and self.username):
            raise InvalidInput("Postgres source needs config.host, config.database and config.username")
        if not self.schemas:
            raise InvalidInput("Postgres source needs at least one non-system schema in config.schemas")
        self._secret_ref = secret_ref
        self._password = password
        self._engine: Engine | None = None

    def _url(self) -> URL:
        password = self._password if self._password is not None else resolve_secret(self._secret_ref)
        query: dict[str, str] = {"connect_timeout": str(self.connect_timeout)}
        if self.sslmode:
            query["sslmode"] = str(self.sslmode)
        return URL.create(
            "postgresql+psycopg",
            username=self.username,
            password=password,
            host=self.host,
            port=self.port,
            database=self.database,
            query=query,
        )

    def sqlalchemy_url(self) -> str:
        return self._url().render_as_string(hide_password=False)

    def _get_engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self._url(), pool_pre_ping=True, pool_size=2, max_overflow=2)
        return self._engine

    def test(self) -> ConnectionTest:
        started = time.perf_counter()
        try:
            with self._get_engine().connect() as conn:
                version = conn.execute(text("SELECT version()")).scalar_one()
                read_only = conn.execute(text("SHOW default_transaction_read_only")).scalar_one()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return ConnectionTest(
                ok=False,
                message=f"Could not connect to {self.host}:{self.port}/{self.database}: {exc.__class__.__name__}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        return ConnectionTest(
            ok=True,
            message=f"Connected to {self.host}:{self.port}/{self.database}",
            latency_ms=int((time.perf_counter() - started) * 1000),
            details={"version": str(version).split(",")[0], "default_transaction_read_only": read_only},
        )

    def discover(self) -> list[DiscoveredAsset]:
        params = {"schemas": self.schemas}
        try:
            with self._get_engine().connect() as conn:
                tables = [dict(r._mapping) for r in conn.execute(text(TABLES_SQL), params)]
                columns = [dict(r._mapping) for r in conn.execute(text(COLUMNS_SQL), params)]
                pks = [dict(r._mapping) for r in conn.execute(text(PRIMARY_KEYS_SQL), params)]
                fks = [dict(r._mapping) for r in conn.execute(text(FOREIGN_KEYS_SQL), params)]
                counts = [dict(r._mapping) for r in conn.execute(text(ROW_COUNTS_SQL), params)]
        except Exception as exc:  # noqa: BLE001
            raise UpstreamUnavailable(f"Postgres discovery failed: {exc.__class__.__name__}: {str(exc).splitlines()[0]}") from None
        return build_assets(tables, columns, pks, fks, counts)

    def extract(self, asset: DiscoveredAsset, *, max_rows: int) -> Iterator[pa.RecordBatch]:
        raise InvalidInput("Postgres sources are queried in place (pushdown); they are not staged")

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
