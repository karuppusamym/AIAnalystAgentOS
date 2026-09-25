"""P4-S03: lineage and table neighbourhood come from Postgres; Neo4j is an optional projection, off
by default. Parity with the Neo4j projection is checked when Neo4j is up (skipped otherwise)."""
from __future__ import annotations

import socket

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # released on close: nothing listens there


@pytest.fixture()
def graph_settings(monkeypatch):
    """Apply env overrides to the cached settings; restore afterwards."""
    from analystos.core.config import get_settings
    from analystos.graph import projection

    def apply(**env: str) -> None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        projection._driver.cache_clear()

    yield apply
    monkeypatch.undo()
    get_settings.cache_clear()
    projection._driver.cache_clear()


@pytest.fixture()
def graph_fixture(control_db):
    """Two workspaces. A: orders -> customers (declared relationship + a lineage joins_to), a dataset
    built from orders, a finding about customers, a relationship to an asset outside the workspace.
    B: the same table name with its own dataset (must never appear in A's neighbourhood)."""
    from analystos.artifacts.registry import link
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Relationship, Source, SourceAsset, User, Workspace

    out: dict = {}
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        for label in ("a", "b"):
            ws = new_id("ws")
            s.add(Workspace(id=ws, name=f"graph {label}", created_by=admin.id))
            s.flush()
            sid = new_id("src")
            schema = f"src_graph_{sid[-6:]}"
            s.add(Source(id=sid, workspace_id=ws, kind="csv", name=f"graph {label}", config={}, status="ready"))
            s.flush()
            ids = {}
            for name in ("orders", "customers"):
                ids[name] = new_id("ast")
                s.add(SourceAsset(id=ids[name], source_id=sid, workspace_id=ws, schema_name=schema, name=name,
                                  source_name=name, kind="table", selected=True))
            s.flush()
            out[label] = {"ws": ws, "schema": schema, "orders": f"{schema}.orders", "customers": f"{schema}.customers", "ids": ids}
        a, b = out["a"], out["b"]
        s.add(Relationship(id=new_id("rel"), workspace_id=a["ws"], from_asset_id=a["ids"]["orders"], from_column="customer_id",
                           to_asset_id=a["ids"]["customers"], to_column="id", cardinality="many_to_one", confidence=0.9))
        s.add(Relationship(id=new_id("rel"), workspace_id=a["ws"], from_asset_id=a["ids"]["orders"], from_column="region_id",
                           to_asset_id="ast_elsewhere", to_column="id", cardinality="many_to_one", confidence=0.5))
        link(s, a["ws"], ("table", a["orders"]), "joins_to", ("table", a["customers"]))
        link(s, a["ws"], ("dataset", "ds_a"), "built_from", ("table", a["orders"]))
        link(s, a["ws"], ("insight", "ins_a"), "about", ("table", a["customers"]))
        link(s, a["ws"], ("table", a["orders"]), "described_by", ("artifact", "art_profile_a"))
        link(s, b["ws"], ("dataset", "ds_b"), "built_from", ("table", a["orders"]))  # same name, other workspace
    return out


def _expected(a: dict) -> list[dict]:
    rows = [
        (a["orders"], "JOINS_TO", "table", a["customers"]),
        (a["orders"], "JOINS_TO", "table", "ast_elsewhere"),
        (a["customers"], "JOINS_TO", "table", a["orders"]),
        (a["orders"], "BUILT_FROM", "dataset", "ds_a"),
        (a["customers"], "ABOUT", "insight", "ins_a"),
        (a["orders"], "DESCRIBED_BY", "artifact", "art_profile_a"),
    ]
    return [{"table": t, "rel": r, "type": ty, "id": i} for t, r, ty, i in sorted(rows)]


def test_neighbourhood_is_served_from_postgres_when_the_graph_is_disabled(graph_fixture, graph_settings):
    from analystos.graph import projection

    graph_settings(ANALYSTOS_GRAPH_ENABLED="false", ANALYSTOS_NEO4J_URI=f"bolt://127.0.0.1:{_closed_port()}")
    a, b = graph_fixture["a"], graph_fixture["b"]
    assert projection.neighborhood([a["orders"], a["customers"]], a["ws"]) == _expected(a)
    assert projection.neighborhood([a["orders"]], b["ws"]) == [
        {"table": a["orders"], "rel": "BUILT_FROM", "type": "dataset", "id": "ds_b"}]
    assert projection.neighborhood([], a["ws"]) == []
    from analystos.db.base import session_scope

    with session_scope() as s:
        assert projection.project_workspace(s, a["ws"])["skipped"] is True


def test_enabled_but_unreachable_neo4j_falls_back_to_postgres(graph_fixture, graph_settings):
    from analystos.graph import projection

    graph_settings(ANALYSTOS_GRAPH_ENABLED="true", ANALYSTOS_NEO4J_URI=f"bolt://127.0.0.1:{_closed_port()}")
    a = graph_fixture["a"]
    assert projection.neighborhood([a["orders"], a["customers"]], a["ws"]) == _expected(a)


def test_health_reports_a_disabled_graph_as_disabled(control_db, graph_settings):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    graph_settings(ANALYSTOS_GRAPH_ENABLED="false", ANALYSTOS_NEO4J_URI=f"bolt://127.0.0.1:{_closed_port()}")
    body = TestClient(app).get("/api/health").json()
    assert body["checks"]["neo4j"]["ok"] is True
    assert body["checks"]["neo4j"]["status"] == "disabled"


def test_postgres_neighbourhood_equals_the_neo4j_projection(graph_fixture, graph_settings):
    from analystos.db.base import session_scope
    from analystos.graph import projection

    graph_settings(ANALYSTOS_GRAPH_ENABLED="true")
    a, b = graph_fixture["a"], graph_fixture["b"]
    with session_scope() as s:
        result = projection.project_workspace(s, a["ws"])
        if not result.get("ok"):
            pytest.skip(f"Neo4j not reachable: {result.get('error')}")
        assert projection.project_workspace(s, b["ws"])["ok"]
    try:
        for ws in (a["ws"], b["ws"]):
            tables = [a["orders"], a["customers"]]
            neo = projection.neo4j_neighborhood(tables, ws)
            with session_scope() as s:
                pg = projection.pg_neighborhood(s, tables, ws)
            assert pg == neo, (ws, pg, neo)
        assert projection.neo4j_neighborhood([a["orders"], a["customers"]], a["ws"]) == _expected(a)
    finally:
        with projection._driver().session() as g:
            g.run("MATCH (n:AOS) WHERE n.workspace_id IN $ws DETACH DELETE n", ws=[a["ws"], b["ws"]])
