"""Fixtures for data-plane integration tests (Postgres + Redis from docker compose).

Import with ``from dataplane_fixtures import *`` after putting ``tests/`` on sys.path. Every
fixture skips cleanly when the infrastructure is unreachable.
"""
from __future__ import annotations

import os
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from analystos.core.config import Settings
from analystos.core.ids import new_id

TEST_DB = os.environ.get("ANALYSTOS_TEST_DP_DB", "analystos_test_dp")  # override per parallel test session
ADMIN_URL = os.environ.get("ANALYSTOS_TEST_ADMIN_URL", "postgresql+psycopg://analystos:analystos@localhost:5432/analystos")


def _pg_reachable() -> bool:
    try:
        engine = create_engine(ADMIN_URL, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


def _redis_reachable(url: str) -> bool:
    try:
        import redis

        return bool(redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1).ping())
    except Exception:  # noqa: BLE001
        return False


def reader_role(settings: Settings, workspace_id: str) -> str:
    from analystos.staging.roles import role_for

    return role_for(settings, workspace_id)


@contextmanager
def reader_as(settings: Settings, workspace_id: str | None):
    """A raw reader-identity connection (no validator, no gateway), switched to a workspace's reader
    role as the gateway does. ``workspace_id=None`` stays the bare reader login."""
    engine = create_engine(settings.analytics_reader_url)
    try:
        with engine.connect() as conn:
            if workspace_id is not None:
                conn.execute(text(f'SET ROLE "{reader_role(settings, workspace_id)}"'))
            yield conn
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def dp_control_url(analytics_plane) -> Iterator[str]:
    """A throwaway control-plane database with all ORM tables."""
    if not _pg_reachable():
        pytest.skip("Postgres (docker compose) is not reachable")
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    admin.dispose()
    url = make_url(ADMIN_URL).set(database=TEST_DB).render_as_string(hide_password=False)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    import analystos.db.models  # noqa: F401 - register tables
    from analystos.db.base import Base

    Base.metadata.create_all(engine)
    engine.dispose()
    yield url
    from analystos.gateway.engines import dispose_all

    dispose_all()
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture(scope="session")
def dp_settings(dp_control_url: str) -> Settings:
    return Settings(database_url=dp_control_url, query_cache_ttl_seconds=120)


@pytest.fixture(scope="session")
def dp_session_factory(dp_control_url: str):
    engine = create_engine(dp_control_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture(scope="session")
def dp_redis(dp_settings: Settings) -> str:
    if not _redis_reachable(dp_settings.redis_url):
        pytest.skip("Redis (docker compose) is not reachable")
    return dp_settings.redis_url


@pytest.fixture(scope="session")
def dp_workspace(dp_session_factory) -> dict[str, str]:
    from analystos.db.models import User, Workspace

    user_id, ws_id = new_id("usr"), new_id("ws")
    with dp_session_factory() as s:
        s.add(User(id=user_id, email=f"{user_id}@test.local", name="DP Test", password_hash="x", is_admin=True))
        s.flush()
        s.add(Workspace(id=ws_id, name="Data plane test", created_by=user_id))
        s.commit()
    return {"user_id": user_id, "workspace_id": ws_id}


@pytest.fixture(scope="session")
def servicenow_connector():
    warnings.filterwarnings("ignore", message=".*starlette.testclient.*")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from fastapi.testclient import TestClient

    from analystos.connectors.servicenow import ServiceNowConnector
    from analystos.connectors.servicenow_mock import app

    client = TestClient(app, base_url="http://servicenow.test")
    return ServiceNowConnector(
        {"instance_url": "http://servicenow.test", "username": "admin", "page_size": 5000},
        http_client=client,
        password="admin",
    )


@pytest.fixture(scope="session")
def staged_servicenow(dp_settings: Settings, dp_session_factory, dp_workspace, servicenow_connector) -> Iterator[dict[str, Any]]:
    """Discover + extract the mock ServiceNow instance, stage it into the analytics DB and register
    the source/asset/column rows in the control plane."""
    from analystos.connectors.naming import staging_schema_for
    from analystos.db.models import Source, SourceAsset, SourceColumn
    from analystos.staging.loader import StagingLoader

    source_id = new_id("sn")
    schema = staging_schema_for(source_id)
    loader = StagingLoader(dp_settings)
    assets = servicenow_connector.discover()
    loads = {}
    for asset in assets:
        loads[asset.name] = loader.load(source_id, asset, servicenow_connector.extract(asset, max_rows=50_000),
                                        workspace_id=dp_workspace["workspace_id"])
    with dp_session_factory() as s:
        s.add(Source(id=source_id, workspace_id=dp_workspace["workspace_id"], kind="servicenow", name="ServiceNow mock",
                     config={"instance_url": "http://servicenow.test"}, status="ready", execution_mode="staged",
                     staging_schema=schema, last_discovered_at=datetime.now(UTC)))
        s.flush()
        for asset in assets:
            aid = new_id("ast")
            s.add(SourceAsset(id=aid, source_id=source_id, workspace_id=dp_workspace["workspace_id"], schema_name=schema,
                              name=asset.name, source_name=asset.source_name, kind=asset.kind, selected=True,
                              row_count=loads[asset.name]["row_count"], freshness_at=asset.freshness_at))
            s.flush()
            for i, c in enumerate(asset.columns):
                s.add(SourceColumn(asset_id=aid, name=c.name, ordinal=i, data_type=c.data_type, is_key=c.is_key,
                                   tags=["pii"] if c.name in ("caller_id", "caller_id_name") else []))
        s.commit()
    yield {"source_id": source_id, "schema": schema, "assets": assets, "loads": loads, "loader": loader}
    loader.drop_source(source_id)


@pytest.fixture()
def servicenow_scope(staged_servicenow, dp_workspace):
    from analystos.contracts.policy import DataScope

    schema = staged_servicenow["schema"]
    sid = staged_servicenow["source_id"]
    assets = [f"{schema}.{a.name}" for a in staged_servicenow["assets"]]
    return DataScope(
        workspace_id=dp_workspace["workspace_id"],
        user_id=dp_workspace["user_id"],
        role="analyst",
        source_ids=[sid],
        assets=assets,
        asset_sources={a: sid for a in assets},
        columns={f"{schema}.{a.name}": [c.name for c in a.columns] for a in staged_servicenow["assets"]},
        denied_columns=[f"{schema}.incident.caller_id", f"{schema}.incident.caller_id_name"],
        source_dialects={sid: "postgres"},
        max_rows=50_000,
        timeout_seconds=30,
    )
