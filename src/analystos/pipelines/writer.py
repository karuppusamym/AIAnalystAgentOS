"""The managed output writer (P6-03, ADR-0011 "Managed output writer", ADR-0014 decision 3).

A fourth analytics identity, separate from the reader (queries), the loader (staged sources) and the
builder (dbt):

  writer   LOGIN NOINHERIT, no privilege of its own. It `SET LOCAL ROLE`s to the workspace writer role
           (`<writer prefix><workspace>`, NOLOGIN), which holds USAGE + CREATE on that workspace's
           allowlisted destination schemas and nothing else: no source schema, no build target, no read of
           any staged data. What it writes comes from the approved candidate snapshot, not from a query.

A destination table is a view over version tables: a candidate is staged as `<table>__v<n>` (COPY, then
its row count, content fingerprint, key uniqueness and non-null keys are checked in the database), and
promotion re-points the view in one transaction. Readers see the old good version or the new one, never
a partial one; an invalid candidate is dropped and never promoted; rollback re-points the view at the
previous version (the rollback pointer). The last `writer_keep_versions` version tables are kept.

Postgres enforces the boundary whatever the code above it says: a write to a source schema is refused
with `permission denied` (tests/integration/test_pipelines.py).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import psycopg
import pyarrow as pa
from psycopg import sql
from sqlalchemy.engine import make_url

from analystos.connectors.naming import is_safe_identifier, workspace_reader_role
from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.core.logging import get_logger
from analystos.staging.loader import _row_converter, _table_fingerprint, pg_type_for
from analystos.staging.roles import ensure_workspace_role, reader_login, role_for

DEFAULT_ENGINE = "postgres:analytics"
VERSION_SEP = "__v"
RESERVED_SCHEMAS = ("public", "information_schema", "analytics")
RESERVED_PREFIXES = ("pg_", "src_")
MAX_TABLE = 50  # room for __v<version>

_log = get_logger(__name__)


def writer_role_for(settings: Any, workspace_id: str) -> str:
    return workspace_reader_role(workspace_id, getattr(settings, "analytics_writer_role_prefix", "analystos_w_"))


def writer_login(settings: Any) -> str:
    name = make_url(settings.analytics_writer_url).username or ""
    if not is_safe_identifier(name):
        raise InvalidInput("analytics writer role name is not a safe identifier")
    return name


def check_identities(settings: Any) -> None:
    """The writer is its own login: never the reader, loader, builder or control-plane identity."""
    writer = make_url(settings.analytics_writer_url)
    others = {make_url(u).username for u in (settings.analytics_reader_url, settings.analytics_loader_url,
                                             settings.analytics_builder_url, settings.database_url)}
    if writer.username in others:
        raise InvalidInput("analytics_writer_url must use the dedicated writer identity, not the reader, loader, "
                           "builder or control-plane identity")
    control = make_url(settings.database_url)
    if (writer.host, writer.port, writer.database) == (control.host, control.port, control.database):
        raise InvalidInput("analytics_writer_url must not point at the control-plane database")


def check_credentials(settings: Any) -> None:
    """Outside development the writer login must not carry the well-known development password."""
    if getattr(settings, "env", "dev") == "dev":
        return
    from analystos.core.config import Settings

    default = make_url(Settings.model_fields["analytics_writer_url"].default).password
    password = make_url(settings.analytics_writer_url).password
    if not password or password == default:
        raise InvalidInput(f"ANALYSTOS_ANALYTICS_WRITER_URL uses the development password in env={settings.env!r}: "
                           "set it from a secret before provisioning destinations or materializing")


def check_destination(schema: str, table: str | None = None, *, forbidden: set[str] = frozenset()) -> None:
    """A destination is a plain schema of its own: never a source's staged schema, a build target, a
    system schema or anything that looks like one."""
    if not is_safe_identifier(schema):
        raise InvalidInput(f"destination schema {schema!r} must be lower snake case ([a-z_][a-z0-9_]*)")
    if schema in forbidden or schema.startswith(RESERVED_PREFIXES) or schema in RESERVED_SCHEMAS:
        raise Forbidden(f"schema {schema} cannot be a destination: sources, build targets and system schemas are "
                        "not written by the managed writer")
    if table is not None and (not is_safe_identifier(table) or len(table) > MAX_TABLE or VERSION_SEP in table):
        raise InvalidInput(f"destination table {table!r} must be lower snake case, at most {MAX_TABLE} characters, "
                           f"without {VERSION_SEP}")


def version_table(table: str, version: int) -> str:
    return f"{table}{VERSION_SEP}{int(version)}"


def _pg(url: str) -> str:
    return make_url(url).render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://")


def _server16(cur: Any) -> bool:
    cur.execute("SHOW server_version_num")
    return int(cur.fetchone()[0]) >= 160000


def ensure_writer_login(settings: Any, *, loader_url: str | None = None) -> bool:
    """Create the writer login when the cluster predates it (01-init.sql creates it on new clusters).
    Returns whether it exists afterwards. Refuses the development password outside development."""
    check_credentials(settings)
    writer = make_url(settings.analytics_writer_url)
    name, db = writer_login(settings), writer.database or ""
    with psycopg.connect(_pg(loader_url or settings.analytics_loader_url), autocommit=True, connect_timeout=5) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is None:
            try:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                    sql.Identifier(name), sql.Literal(writer.password or "")))
            except psycopg.errors.InsufficientPrivilege:
                _log.warning("the loader may not create the writer login %s; apply deploy/postgres/01-init.sql", name)
                return False
            except (psycopg.errors.DuplicateObject, psycopg.errors.UniqueViolation):
                pass
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), sql.Identifier(name)))
    return True


def provision_destination(settings: Any, workspace_id: str, schema: str, *, forbidden: set[str] = frozenset(),
                          loader_url: str | None = None) -> dict[str, Any]:
    """Create (idempotently) the workspace writer role and the destination schema; grant the role CREATE on
    that schema only, and make what it writes readable by the workspace reader role."""
    check_identities(settings)
    check_destination(schema, forbidden=forbidden)
    reader, writer = reader_login(settings), writer_login(settings)
    reader_role, writer_role = role_for(settings, workspace_id), writer_role_for(settings, workspace_id)
    for ident in (reader, writer, reader_role, writer_role):
        if not is_safe_identifier(ident):
            raise InvalidInput(f"unsafe identifier {ident!r}")
    ensure_writer_login(settings, loader_url=loader_url)
    w, r, s = sql.Identifier(writer_role), sql.Identifier(reader_role), sql.Identifier(schema)
    with psycopg.connect(_pg(loader_url or settings.analytics_loader_url), connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s", (schema,))
        existing = cur.fetchone()
        cur.execute("SELECT current_user")
        loader = cur.fetchone()[0]
        if existing is not None and existing[0].strip('"') != loader:
            raise Forbidden(f"schema {schema} exists and is owned by {existing[0]}; a destination must be created by "
                            "the platform")
        ensure_workspace_role(cur, reader_role, reader)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (writer_role,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(w))
        if _server16(cur):
            cur.execute(sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(w, sql.Identifier(writer)))
        else:
            cur.execute(sql.SQL("GRANT {} TO {}").format(w, sql.Identifier(writer)))
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(s))
        cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(s))
        cur.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(s, w))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, r))
        conn.commit()
    with psycopg.connect(_pg(settings.analytics_writer_url), connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(w))
        cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(s, r))
        conn.commit()
    return {"schema": schema, "writer_role": writer_role, "reader_role": reader_role, "writer_login": writer,
            "grants": [f"USAGE, CREATE ON SCHEMA {schema} TO {writer_role}", f"{writer_role} TO {writer} (SET only)",
                       f"default SELECT on {schema} tables and views TO {reader_role}"]}


class ManagedWriter:
    """Stages, validates, promotes and rolls back destination versions as the workspace writer role."""

    def __init__(self, settings: Any) -> None:
        check_identities(settings)
        check_credentials(settings)
        self.settings = settings

    def _connect(self, workspace_id: str) -> tuple[Any, str]:
        role = writer_role_for(self.settings, workspace_id)
        if not is_safe_identifier(role):
            raise InvalidInput(f"unsafe identifier {role!r}")
        return psycopg.connect(_pg(self.settings.analytics_writer_url), connect_timeout=5), role

    @staticmethod
    def _begin(cur: Any, role: str, schema: str, table: str) -> None:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))  # this transaction only
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"aos.writer:{schema}.{table}",))

    def stage(self, workspace_id: str, schema: str, table: str, version: int,
              batches: Iterable[pa.RecordBatch]) -> dict[str, Any]:
        """COPY the candidate into its own version table (a leftover of a failed attempt is replaced; a
        version the view reads cannot be dropped, so a promoted version is never overwritten)."""
        check_destination(schema, table)
        vt = version_table(table, version)
        conn, role = self._connect(workspace_id)
        with conn, conn.cursor() as cur:
            self._begin(cur, role, schema, table)
            batches = list(batches)
            arrow_schema = batches[0].schema if batches else None
            if arrow_schema is None:
                raise InvalidInput("nothing to stage: the candidate has no schema")
            names, types = list(arrow_schema.names), [pg_type_for(f.type) for f in arrow_schema]
            ident = sql.Identifier(schema, vt)
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(ident))
            cur.execute(sql.SQL("CREATE TABLE {} ({})").format(ident, sql.SQL(", ").join(
                sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(t)) for n, t in zip(names, types, strict=True))))
            convert = _row_converter(types)
            copied = 0
            with cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(ident, sql.SQL(", ").join(
                    sql.Identifier(n) for n in names))) as copy:
                for batch in batches:
                    cols = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
                    for values in zip(*cols, strict=True):
                        copy.write_row(convert(list(values)))
                        copied += 1
            fp, rows = _table_fingerprint(cur, ident, names, types)
            conn.commit()
        return {"version_table": vt, "row_count": rows, "copied_rows": copied, "content_fingerprint": fp,
                "columns": [{"name": n, "type": t} for n, t in zip(names, types, strict=True)]}

    def validate(self, workspace_id: str, schema: str, table: str, version: int, *, keys: list[str],
                 expected_rows: int) -> list[dict[str, Any]]:
        """Checks of the staged version in the database, as the writer: the approved row count, and that
        the declared keys are unique and not null. A failed check blocks promotion."""
        vt = version_table(table, version)
        conn, role = self._connect(workspace_id)
        out: list[dict[str, Any]] = []
        with conn, conn.cursor() as cur:
            self._begin(cur, role, schema, table)
            ident = sql.Identifier(schema, vt)
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(ident))
            n = int(cur.fetchone()[0])
            out.append({"check": "row_count", "expected": expected_rows, "observed": n, "ok": n == expected_rows})
            if keys:
                cols = sql.SQL(", ").join(sql.Identifier(k) for k in keys)
                cur.execute(sql.SQL("SELECT count(*) FROM (SELECT {} FROM {} GROUP BY {} HAVING count(*) > 1) d")
                            .format(cols, ident, cols))
                dup = int(cur.fetchone()[0])
                out.append({"check": "key_unique", "columns": keys, "duplicate_keys": dup, "ok": dup == 0})
                nulls = sql.SQL(" OR ").join(sql.SQL("{} IS NULL").format(sql.Identifier(k)) for k in keys)
                cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE {}").format(ident, nulls))
                null_rows = int(cur.fetchone()[0])
                out.append({"check": "key_not_null", "columns": keys, "null_rows": null_rows, "ok": null_rows == 0})
            conn.rollback()
        return out

    def promote(self, workspace_id: str, schema: str, table: str, version: int, columns: list[str]) -> dict[str, Any]:
        """Re-point the destination view at a version table, atomically (one transaction)."""
        check_destination(schema, table)
        vt = version_table(table, version)
        conn, role = self._connect(workspace_id)
        with conn, conn.cursor() as cur:
            self._begin(cur, role, schema, table)
            cur.execute("SELECT to_regclass(%s)", (f'"{schema}"."{vt}"',))
            if cur.fetchone()[0] is None:
                raise Conflict(f"version table {schema}.{vt} does not exist (dropped by retention?); stage it again")
            view = sql.Identifier(schema, table)
            cur.execute("SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = %s AND c.relname = %s", (schema, table))
            kind = cur.fetchone()
            if kind is not None and kind[0] != "v":
                raise Conflict(f"{schema}.{table} exists and is not a managed view; refusing to replace it")
            cur.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(view))
            cur.execute(sql.SQL("CREATE VIEW {} AS SELECT {} FROM {}").format(
                view, sql.SQL(", ").join(sql.Identifier(c) for c in columns), sql.Identifier(schema, vt)))
            conn.commit()
        return {"view": f"{schema}.{table}", "version_table": f"{schema}.{vt}"}

    def drop_versions(self, workspace_id: str, schema: str, table: str, versions: list[int]) -> list[str]:
        """Drop version tables (a rejected candidate, or retention). The one the view reads cannot be
        dropped (a dependent view) and is skipped."""
        conn, role = self._connect(workspace_id)
        dropped = []
        with conn, conn.cursor() as cur:
            self._begin(cur, role, schema, table)
            for v in versions:
                vt = version_table(table, v)
                cur.execute("SAVEPOINT drop_one")
                try:
                    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(schema, vt)))
                    cur.execute("RELEASE SAVEPOINT drop_one")
                    dropped.append(vt)
                except psycopg.errors.DependentObjectsStillExist:
                    cur.execute("ROLLBACK TO SAVEPOINT drop_one")
            conn.commit()
        return dropped
