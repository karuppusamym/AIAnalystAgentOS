"""Unit-test support: an in-memory SQLite control plane, so engine and event code can be tested
without the compose stack. Postgres-only column types are rendered as their SQLite equivalents."""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"  # SQLite only autoincrements INTEGER PRIMARY KEY


@pytest.fixture
def sqlite_db(monkeypatch):
    """Point `session_scope` (and the API `db` dependency) at a fresh in-memory SQLite database."""
    from analystos.db import base, models
    from analystos.events import bus

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
                           json_serializer=base.json_dumps)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=OFF")

    tables = [models.Base.metadata.tables[t] for t in (
        "app_user", "workspace", "workspace_member", "analysis_run", "run_task", "run_event", "approval", "hypothesis",
        "insight", "artifact", "artifact_version", "audit_event", "monitor", "alert", "notification")]
    models.Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(base, "SessionLocal", lambda: factory())
    monkeypatch.setattr(bus, "_redis", False)  # no Redis nudges from unit tests
    yield factory
    engine.dispose()
