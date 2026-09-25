"""Per-workspace reader roles on the analytics database (defence in depth between workspaces).

Each workspace gets a NOLOGIN role (``<prefix><workspace id>``) that holds USAGE/SELECT on that
workspace's staged ``src_*`` schemas only. The reader login is a member *without* inheritance
(``INHERIT FALSE, SET TRUE``), so the reader identity alone reads no staged table; the gateway
switches to the owning workspace's role with ``SET LOCAL ROLE`` inside its read-only transaction.
A validator bug can therefore no longer read another workspace's snapshot.

Only the loader identity runs this (it owns the schemas and needs CREATEROLE; see
``deploy/postgres/01-init.sql`` and ``provision_loader_createrole``).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from analystos.connectors.naming import is_safe_identifier, staging_schema_for, workspace_reader_role
from analystos.core.errors import Forbidden, InvalidInput
from analystos.core.logging import get_logger
from analystos.gateway.engines import get_engine

_log = get_logger(__name__)


def role_for(settings: Any, workspace_id: str) -> str:
    return workspace_reader_role(workspace_id, getattr(settings, "analytics_workspace_role_prefix", "analystos_r_"))


def reader_login(settings: Any) -> str:
    name = make_url(settings.analytics_reader_url).username or "analystos_reader"
    if not is_safe_identifier(name):
        raise InvalidInput("analytics reader role name is not a safe identifier")
    return name


def ensure_workspace_role(cur: Any, role: str, reader: str) -> None:
    """Create the NOLOGIN role if missing and make the reader a non-inheriting member of it."""
    for ident in (role, reader):
        if not is_safe_identifier(ident):
            raise InvalidInput(f"Unsafe identifier {ident!r}")
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if cur.fetchone() is None:
        cur.execute("SAVEPOINT aos_role")
        try:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(role)))
            cur.execute("RELEASE SAVEPOINT aos_role")
        except (psycopg.errors.DuplicateObject, psycopg.errors.UniqueViolation):
            cur.execute("ROLLBACK TO SAVEPOINT aos_role")  # a concurrent load created it
        except psycopg.errors.InsufficientPrivilege as exc:
            raise Forbidden(
                "The analytics loader identity cannot create per-workspace reader roles (it needs CREATEROLE). "
                "Run `analystos migrate` with an administrative control-plane identity, or apply "
                "deploy/postgres/01-init.sql."
            ) from exc
    cur.execute("SHOW server_version_num")
    if int(cur.fetchone()[0]) >= 160000:
        cur.execute(sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(sql.Identifier(role), sql.Identifier(reader)))
    else:  # older servers: the reader login itself is NOINHERIT (01-init.sql)
        cur.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(reader)))


def grant_schema(cur: Any, schema: str, role: str, reader: str) -> None:
    """The workspace role reads the schema; the reader login keeps no direct privilege on it."""
    for ident in (schema, role, reader):
        if not is_safe_identifier(ident):
            raise InvalidInput(f"Unsafe identifier {ident!r}")
    s, r = sql.Identifier(schema), sql.Identifier(role)
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, r))
    cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(s, r))
    revoke_reader(cur, schema, reader)


def revoke_reader(cur: Any, schema: str, reader: str) -> None:
    s, rd = sql.Identifier(schema), sql.Identifier(reader)
    cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(s, rd))
    cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(s, rd))


def backfill(settings: Any, staged: Iterable[tuple[str, str]], *, loader_url: str | None = None) -> dict[str, Any]:
    """Move existing staged schemas to per-workspace roles.

    ``staged``: (source_id, workspace_id) of every staged source in the control plane. Schemas that
    exist in the analytics DB but belong to no known source lose the reader's direct grants and get
    no workspace role (fail closed). Idempotent; run by ``analystos migrate``."""
    reader = reader_login(settings)
    owners = {staging_schema_for(src): ws for src, ws in staged}
    raw = get_engine(loader_url or settings.analytics_loader_url).raw_connection()
    moved, orphaned = [], []
    try:
        conn = raw.driver_connection
        with conn.cursor() as cur:
            cur.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'src\\_%' ORDER BY nspname")
            for (schema,) in cur.fetchall():
                if not is_safe_identifier(schema):
                    continue
                ws = owners.get(schema)
                if ws is None:
                    revoke_reader(cur, schema, reader)
                    orphaned.append(schema)
                    continue
                role = role_for(settings, ws)
                ensure_workspace_role(cur, role, reader)
                grant_schema(cur, schema, role, reader)
                moved.append({"schema": schema, "role": role})
        conn.commit()
    except Exception:
        raw.driver_connection.rollback()
        raise
    finally:
        raw.close()
    _log.info("analytics roles backfilled: %d schemas moved, %d orphaned", len(moved), len(orphaned))
    return {"moved": moved, "orphaned": orphaned}


def provision_loader_createrole(admin_url: str, loader: str) -> bool:
    """Self-healing for clusters initialised before per-workspace roles: give the loader CREATEROLE
    when the administrative (control-plane) identity is allowed to. Returns whether it holds it."""
    if not is_safe_identifier(loader):
        raise InvalidInput("analytics loader role name is not a safe identifier")
    url = make_url(admin_url).render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        row = conn.execute("SELECT rolcreaterole FROM pg_roles WHERE rolname = %s", (loader,)).fetchone()
        if row is None:
            return False
        if row[0]:
            return True
        try:
            conn.execute(sql.SQL("ALTER ROLE {} CREATEROLE").format(sql.Identifier(loader)))
            return True
        except psycopg.errors.InsufficientPrivilege:
            _log.warning("control-plane identity may not grant CREATEROLE to %s; per-workspace roles need it", loader)
            return False
