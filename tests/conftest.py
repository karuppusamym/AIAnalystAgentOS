"""Shared test setup. Control-plane tests run against a throwaway Postgres database
(`analystos_test`) on the compose Postgres; they skip cleanly when it is unreachable."""
from __future__ import annotations

import os

TEST_DB = os.environ.get("ANALYSTOS_TEST_DATABASE_URL", "postgresql+psycopg://analystos:analystos@localhost:5432/analystos_test")
os.environ["ANALYSTOS_DATABASE_URL"] = TEST_DB
os.environ.setdefault("ANALYSTOS_ORCHESTRATOR", "local")
os.environ.setdefault("ANALYSTOS_JWT_SECRET", "test-secret-test-secret-test-secret-123")

import pytest  # noqa: E402


def _admin_url() -> str:
    return TEST_DB.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="session")
def control_db():
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
