"""The build identity and designated target schemas on the analytics database (P4-E06).

Three analytics identities, three jobs:

  loader   owns the database and the staged `src_*` schemas; creates roles and target schemas.
  reader   the query gateway's login; reads a workspace's schemas only through `SET LOCAL ROLE`.
  builder  the BuildGateway's login (LOGIN NOINHERIT, no privilege of its own). dbt connects as it
           and `SET ROLE`s to the workspace build role (`<build prefix><workspace>`, NOLOGIN), which
           holds USAGE + CREATE on that workspace's designated target schemas and nothing else, and
           reads the workspace's staged schemas as a member of the workspace reader role.

So a model that writes outside a target schema, or into a source schema, is refused by Postgres
itself, whatever the generated project says; the BuildGateway's static checks refuse it earlier.
Built relations are readable by the workspace reader role (default privileges), so the query
gateway and Superset can use them like any other governed table.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from analystos.connectors.naming import is_safe_identifier, workspace_reader_role
from analystos.core.errors import Forbidden, InvalidInput
from analystos.core.logging import get_logger
from analystos.staging.roles import ensure_workspace_role, reader_login, role_for

DEFAULT_ENGINE = "postgres:analytics"
ENGINES = (DEFAULT_ENGINE,)
RESERVED_SCHEMAS = ("public", "information_schema", "analytics")
RESERVED_PREFIXES = ("pg_", "src_")

_log = get_logger(__name__)


def build_role_for(settings: Any, workspace_id: str) -> str:
    return workspace_reader_role(workspace_id, getattr(settings, "analytics_build_role_prefix", "analystos_b_"))


def builder_login(settings: Any) -> str:
    name = make_url(settings.analytics_builder_url).username or ""
    if not is_safe_identifier(name):
        raise InvalidInput("analytics builder role name is not a safe identifier")
    return name


def check_identities(settings: Any) -> None:
    """The build identity is its own login: never the reader, the loader or the control plane."""
    builder = make_url(settings.analytics_builder_url)
    others = {make_url(u).username for u in (settings.analytics_reader_url, settings.analytics_loader_url, settings.database_url)}
    if builder.username in others:
        raise InvalidInput("analytics_builder_url must use the dedicated build identity, not the reader/loader/control identity")
    control = make_url(settings.database_url)
    if (builder.host, builder.port, builder.database) == (control.host, control.port, control.database):
        raise InvalidInput("analytics_builder_url must not point at the control-plane database")


def check_schema_name(schema: str, *, source_schemas: set[str] = frozenset()) -> None:
    """A designated target is a plain new schema: never a source's staged schema, a system schema or
    anything that looks like one."""
    if not is_safe_identifier(schema):
        raise InvalidInput(f"target schema {schema!r} must be lower snake case ([a-z_][a-z0-9_]*, at most 63 characters)")
    if schema in source_schemas or schema.startswith(RESERVED_PREFIXES) or schema in RESERVED_SCHEMAS:
        raise Forbidden(f"schema {schema} cannot be a build target: sources and system schemas stay read-only")


def _pg(url: str) -> str:
    return make_url(url).render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://")


def _server16(cur: Any) -> bool:
    cur.execute("SHOW server_version_num")
    return int(cur.fetchone()[0]) >= 160000


def ensure_builder_login(settings: Any, *, loader_url: str | None = None) -> bool:
    """Create the builder login if the cluster predates it (01-init.sql creates it on new clusters).
    Returns whether it exists afterwards."""
    builder = make_url(settings.analytics_builder_url)
    name, db = builder_login(settings), builder.database or ""
    with psycopg.connect(_pg(loader_url or settings.analytics_loader_url), autocommit=True, connect_timeout=5) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is None:
            try:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                    sql.Identifier(name), sql.Literal(builder.password or "")))
            except psycopg.errors.InsufficientPrivilege:
                _log.warning("the loader may not create the build login %s; apply deploy/postgres/01-init.sql", name)
                return False
            except (psycopg.errors.DuplicateObject, psycopg.errors.UniqueViolation):
                pass
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), sql.Identifier(name)))
    return True


def provision_target(settings: Any, workspace_id: str, schema: str, *, source_schemas: set[str] = frozenset(),
                     loader_url: str | None = None) -> dict[str, Any]:
    """Create (idempotently) the workspace build role and the target schema, grant the role CREATE on
    that schema only, and make what it builds readable by the workspace reader role."""
    check_schema_name(schema, source_schemas=source_schemas)
    reader, builder = reader_login(settings), builder_login(settings)
    reader_role, build_role = role_for(settings, workspace_id), build_role_for(settings, workspace_id)
    for ident in (reader, builder, reader_role, build_role):
        if not is_safe_identifier(ident):
            raise InvalidInput(f"unsafe identifier {ident!r}")
    ensure_builder_login(settings, loader_url=loader_url)
    b, r, s = sql.Identifier(build_role), sql.Identifier(reader_role), sql.Identifier(schema)
    with psycopg.connect(_pg(loader_url or settings.analytics_loader_url), connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s", (schema,))
        existing = cur.fetchone()
        cur.execute("SELECT current_user")
        loader = cur.fetchone()[0]
        if existing is not None and existing[0].strip('"') != loader:
            raise Forbidden(f"schema {schema} exists and is owned by {existing[0]}; a build target must be created by the platform")
        ensure_workspace_role(cur, reader_role, reader)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (build_role,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN INHERIT").format(b))
        cur.execute(sql.SQL("GRANT {} TO {}").format(r, b))  # reads the workspace's staged schemas, never writes them
        if _server16(cur):
            cur.execute(sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(b, sql.Identifier(builder)))
        else:
            cur.execute(sql.SQL("GRANT {} TO {}").format(b, sql.Identifier(builder)))
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(s))
        cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(s))
        cur.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(s, b))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, r))
        conn.commit()
    # As the build role itself: what it creates in the target is readable by the workspace reader role.
    with psycopg.connect(_pg(settings.analytics_builder_url), connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SET ROLE {}").format(b))
        cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(s, r))
        conn.commit()
    return {"schema": schema, "build_role": build_role, "reader_role": reader_role, "builder_login": builder,
            "grants": [f"USAGE, CREATE ON SCHEMA {schema} TO {build_role}", f"{reader_role} TO {build_role}",
                       f"{build_role} TO {builder} (SET only)", f"default SELECT on {schema} tables TO {reader_role}"]}
