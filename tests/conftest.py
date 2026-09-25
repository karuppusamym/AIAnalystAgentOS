"""Shared test setup. Control-plane tests run against a throwaway Postgres database
(`analystos_test`) on the compose Postgres; they skip cleanly when it is unreachable.

The analytics plane is isolated too: integration tests get their own analytics database and their
own loader/reader logins (``aostest<suffix>_loader`` / ``_reader``, the loader with CREATEROLE so it
can create the per-workspace reader roles ``aostest<suffix>_r_<ws>``). Everything is created by the
``analytics_plane`` fixture and dropped afterwards, so parallel sessions (``ANALYSTOS_TEST_DP_DB``
suffix) never share roles or staged schemas, and the shared ``analytics`` database is never touched."""
from __future__ import annotations

import os

TEST_DB = os.environ.get("ANALYSTOS_TEST_DATABASE_URL", "postgresql+psycopg://analystos:analystos@localhost:5432/analystos_test")
os.environ["ANALYSTOS_DATABASE_URL"] = TEST_DB
os.environ.setdefault("ANALYSTOS_ORCHESTRATOR", "local")
os.environ.setdefault("ANALYSTOS_JWT_SECRET", "test-secret-test-secret-test-secret-123")

_DP_DB = os.environ.get("ANALYSTOS_TEST_DP_DB", "analystos_test_dp")
TEST_SUFFIX = _DP_DB.removeprefix("analystos_test_dp").strip("_")
ROLE_PREFIX = f"aostest_{TEST_SUFFIX}_" if TEST_SUFFIX else "aostest_"
ANALYTICS_DB = f"{_DP_DB}_analytics"
_HOSTPORT = TEST_DB.split("@", 1)[1].rsplit("/", 1)[0]
os.environ["ANALYSTOS_ANALYTICS_LOADER_URL"] = f"postgresql+psycopg://{ROLE_PREFIX}loader:loader@{_HOSTPORT}/{ANALYTICS_DB}"
os.environ["ANALYSTOS_ANALYTICS_READER_URL"] = f"postgresql+psycopg://{ROLE_PREFIX}reader:reader@{_HOSTPORT}/{ANALYTICS_DB}"
os.environ["ANALYSTOS_ANALYTICS_WORKSPACE_ROLE_PREFIX"] = f"{ROLE_PREFIX}r_"
# Superset reaches the same cluster from inside the compose network.
os.environ["ANALYSTOS_SUPERSET_ANALYTICS_SQLALCHEMY_URI"] = f"postgresql+psycopg2://{ROLE_PREFIX}reader:reader@postgres:5432/{ANALYTICS_DB}"

import pytest  # noqa: E402


def _admin_url() -> str:
    return TEST_DB.rsplit("/", 1)[0] + "/postgres"


def _drop_analytics_plane(conn) -> None:  # noqa: ANN001
    from sqlalchemy import text

    conn.execute(text(f'DROP DATABASE IF EXISTS "{ANALYTICS_DB}" WITH (FORCE)'))
    like = ROLE_PREFIX.replace("_", "\\_") + "%"
    roles = [r[0] for r in conn.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE :p"), {"p": like})]
    # Workspace roles first (their memberships were granted by the loader), then the logins.
    for role in sorted(roles, key=lambda r: (r in (f"{ROLE_PREFIX}loader", f"{ROLE_PREFIX}reader"), r)):
        conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))


@pytest.fixture(scope="session")
def analytics_plane():
    """Throwaway analytics DB + test-prefixed loader/reader logins, mirroring deploy/postgres/01-init.sql."""
    from sqlalchemy import create_engine, text

    try:
        admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
        with admin.connect() as c:
            _drop_analytics_plane(c)
            c.execute(text(f"CREATE ROLE {ROLE_PREFIX}loader LOGIN CREATEROLE PASSWORD 'loader'"))
            c.execute(text(f"CREATE ROLE {ROLE_PREFIX}reader LOGIN NOINHERIT PASSWORD 'reader'"))
            c.execute(text(f"ALTER ROLE {ROLE_PREFIX}reader SET default_transaction_read_only = on"))
            c.execute(text(f"ALTER ROLE {ROLE_PREFIX}reader SET statement_timeout = '60s'"))
            c.execute(text(f'CREATE DATABASE "{ANALYTICS_DB}" OWNER {ROLE_PREFIX}loader'))
            c.execute(text(f'REVOKE CONNECT ON DATABASE "{ANALYTICS_DB}" FROM PUBLIC'))
            c.execute(text(f'GRANT CONNECT ON DATABASE "{ANALYTICS_DB}" TO {ROLE_PREFIX}loader, {ROLE_PREFIX}reader'))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres unavailable: {exc}")
    yield {"db": ANALYTICS_DB, "prefix": ROLE_PREFIX, "loader": f"{ROLE_PREFIX}loader", "reader": f"{ROLE_PREFIX}reader",
           "admin_url": _admin_url()}
    from analystos.gateway.engines import dispose_all

    dispose_all()
    with admin.connect() as c:
        _drop_analytics_plane(c)
    admin.dispose()


@pytest.fixture(scope="session")
def control_db(analytics_plane):
    """Fresh control-plane schema for the test session."""
    from sqlalchemy import create_engine, text

    try:
        admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            name = TEST_DB.rsplit("/", 1)[1]
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}' AND pid <> pg_backend_pid()"))
            c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
            c.execute(text(f"CREATE DATABASE {name}"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres unavailable: {exc}")
    engine = create_engine(TEST_DB)
    with engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    from analystos.db import models  # noqa: F401
    from analystos.db.base import Base

    Base.metadata.create_all(engine)
    from analystos.cli import seed

    seed()
    yield TEST_DB
    engine.dispose()
