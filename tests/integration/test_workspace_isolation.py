"""P4-C03: isolation between workspaces does not rest on the SQL validator alone.

Negative tests across two workspaces that stage a table under the same name:
  * the reader login alone reads no staged table; workspace B's reader role cannot read A's table
    even with raw SQL (no validator, no gateway);
  * the gateway, with its validator bypassed, still cannot read across workspaces, because it runs
    staged SQL as the owning workspace's role;
  * existing schemas are moved to workspace roles by the backfill;
  * the same name-identified lineage edge exists in both workspaces;
  * the Neo4j neighbourhood never crosses workspaces (skipped when Neo4j is down).
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import func, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.errors import Forbidden  # noqa: E402
from analystos.core.ids import new_id  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403
from dataplane_fixtures import reader_as  # noqa: E402

pytestmark = pytest.mark.integration


def _orders(label: str) -> pa.Table:
    return pa.table({"id": pa.array([1, 2], pa.int64()), "secret": pa.array([f"{label}-1", f"{label}-2"])})


@pytest.fixture()
def two_workspaces(dp_settings, dp_session_factory, dp_workspace):
    from analystos.connectors.naming import staging_schema_for
    from analystos.db.models import Source, SourceAsset, Workspace
    from analystos.staging.loader import StagingLoader

    ws_a = dp_workspace["workspace_id"]
    ws_b = new_id("ws")
    with dp_session_factory() as s:
        s.add(Workspace(id=ws_b, name="Isolation B", created_by=dp_workspace["user_id"]))
        s.commit()
    loader = StagingLoader(dp_settings)
    out = {}
    for label, ws in (("a", ws_a), ("b", ws_b)):
        sid = new_id(f"iso{label}")
        schema = staging_schema_for(sid)
        loader.load(sid, "orders", iter(_orders(label).to_batches()), workspace_id=ws)
        with dp_session_factory() as s:
            s.add(Source(id=sid, workspace_id=ws, kind="csv", name=f"iso {label}", config={}, status="ready",
                         execution_mode="staged", staging_schema=schema, last_discovered_at=datetime.now(UTC)))
            s.flush()
            s.add(SourceAsset(id=new_id("ast"), source_id=sid, workspace_id=ws, schema_name=schema, name="orders",
                              source_name="orders", kind="table", selected=True, row_count=2))
            s.commit()
        out[label] = {"ws": ws, "source": sid, "schema": schema, "table": f"{schema}.orders"}
    out["user"] = dp_workspace["user_id"]
    yield out
    for label in ("a", "b"):
        loader.drop_source(out[label]["source"])


def _scope(ws: dict, user_id: str):
    from analystos.contracts.policy import DataScope

    return DataScope(workspace_id=ws["ws"], user_id=user_id, role="analyst", source_ids=[ws["source"]], assets=[ws["table"]],
                     asset_sources={ws["table"]: ws["source"]}, columns={ws["table"]: ["id", "secret"]},
                     source_dialects={ws["source"]: "postgres"}, max_rows=100, timeout_seconds=10)


def test_reader_login_alone_reads_no_staged_table(dp_settings, two_workspaces):
    for label in ("a", "b"):
        with reader_as(dp_settings, None) as conn, pytest.raises(Exception, match="permission denied"):  # noqa: PT011
            conn.execute(text(f"SELECT secret FROM {two_workspaces[label]['table']}"))


def test_workspace_b_cannot_read_a_staged_table_even_with_raw_sql(dp_settings, two_workspaces):
    a, b = two_workspaces["a"], two_workspaces["b"]
    with reader_as(dp_settings, b["ws"]) as conn:
        assert [r[0] for r in conn.execute(text(f"SELECT secret FROM {b['table']} ORDER BY id"))] == ["b-1", "b-2"]
    with reader_as(dp_settings, b["ws"]) as conn, pytest.raises(Exception, match="permission denied"):  # noqa: PT011
        conn.execute(text(f"SELECT secret FROM {a['table']}"))
    with reader_as(dp_settings, a["ws"]) as conn:  # and the other way round
        assert conn.execute(text(f"SELECT count(*) FROM {a['table']}")).scalar_one() == 2
    with reader_as(dp_settings, a["ws"]) as conn, pytest.raises(Exception, match="permission denied"):  # noqa: PT011
        conn.execute(text(f"SELECT secret FROM {b['table']}"))


def test_gateway_with_validator_bypassed_still_cannot_cross_workspaces(dp_settings, dp_session_factory, two_workspaces,
                                                                     monkeypatch):
    """Simulate a validator bug: B's scope, B's source, but SQL that reads A's schema."""
    from analystos.db.models import QueryExecution
    from analystos.gateway import service
    from analystos.gateway.types import ValidatedSQL

    a, b = two_workspaces["a"], two_workspaces["b"]
    gw = service.QueryGateway(dp_settings, session_factory=dp_session_factory)
    ok = gw.execute(_scope(b, two_workspaces["user"]), "SELECT secret FROM orders ORDER BY id", actor="user:test", use_cache=False)
    assert ok.rows == [["b-1"], ["b-2"]]

    def buggy_validator(scope, sql, *, max_rows):  # noqa: ANN001, ANN202
        return ValidatedSQL(original_sql=sql, executable_sql=f"SELECT secret FROM {a['table']}", dialect="postgres",
                            source_id=b["source"], referenced_assets=[b["table"]], fingerprint="bypassed")

    monkeypatch.setattr(service, "validate_sql", buggy_validator)
    with pytest.raises(Forbidden, match="permission denied"):
        gw.execute(_scope(b, two_workspaces["user"]), "SELECT secret FROM orders", actor="user:test", use_cache=False)
    with dp_session_factory() as s:
        last = s.scalar(select(QueryExecution).where(QueryExecution.workspace_id == b["ws"], QueryExecution.fingerprint == "bypassed"))
        assert last is not None and last.status == "error" and last.row_count == 0


def test_backfill_moves_legacy_grants_to_workspace_roles(dp_settings, two_workspaces):
    """A schema staged before per-workspace roles (direct grant to the reader) is moved over; a schema
    no control-plane source owns loses the reader grant and gains no role (fail closed)."""
    from psycopg import sql as psql

    from analystos.gateway.engines import get_engine
    from analystos.staging.roles import backfill, reader_login

    reader = reader_login(dp_settings)
    legacy, orphan = f"src_legacy_{new_id('x')[-8:]}", f"src_orphan_{new_id('x')[-8:]}"
    raw = get_engine(dp_settings.analytics_loader_url).raw_connection()
    try:
        with raw.driver_connection.cursor() as cur:
            for schema in (legacy, orphan):
                cur.execute(psql.SQL("CREATE SCHEMA {}").format(psql.Identifier(schema)))
                cur.execute(psql.SQL("CREATE TABLE {} AS SELECT 1 AS x").format(psql.Identifier(schema, "t")))
                cur.execute(psql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(psql.Identifier(schema), psql.Identifier(reader)))
                cur.execute(psql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(psql.Identifier(schema),
                                                                                             psql.Identifier(reader)))
        raw.driver_connection.commit()
        with reader_as(dp_settings, None) as conn:  # the legacy state: the bare reader reads it
            assert conn.execute(text(f"SELECT x FROM {legacy}.t")).scalar_one() == 1
        a = two_workspaces["a"]
        result = backfill(dp_settings, [(legacy.removeprefix("src_"), a["ws"]), (a["source"], a["ws"]),
                                        (two_workspaces["b"]["source"], two_workspaces["b"]["ws"])])
        assert {m["schema"] for m in result["moved"]} >= {legacy, a["schema"], two_workspaces["b"]["schema"]}
        assert orphan in result["orphaned"]
        for schema in (legacy, orphan):
            with reader_as(dp_settings, None) as conn, pytest.raises(Exception, match="permission denied"):  # noqa: PT011
                conn.execute(text(f"SELECT x FROM {schema}.t"))
        with reader_as(dp_settings, a["ws"]) as conn:
            assert conn.execute(text(f"SELECT x FROM {legacy}.t")).scalar_one() == 1
        with reader_as(dp_settings, two_workspaces["b"]["ws"]) as conn, pytest.raises(Exception, match="permission denied"):  # noqa: PT011
            conn.execute(text(f"SELECT x FROM {legacy}.t"))
        assert backfill(dp_settings, [])["orphaned"]  # idempotent, and unknown schemas stay closed
    finally:
        with raw.driver_connection.cursor() as cur:
            for schema in (legacy, orphan):
                cur.execute(psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psql.Identifier(schema)))
        raw.driver_connection.commit()
        raw.close()


def test_loader_without_createrole_gets_a_clear_error_and_migrate_heals_it(dp_settings, analytics_plane):
    """Clusters initialised before this change: the loader lacks CREATEROLE until `analystos migrate`."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from analystos.staging.roles import provision_loader_createrole

    role = f"{analytics_plane['prefix']}oldloader"
    admin = create_engine(analytics_plane["admin_url"], isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'x'"))
    try:
        admin_url = make_url(analytics_plane["admin_url"]).render_as_string(hide_password=False)
        assert provision_loader_createrole(admin_url, role) is True
        with admin.connect() as c:
            assert c.execute(text("SELECT rolcreaterole FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar_one()
        assert provision_loader_createrole(admin_url, f"{analytics_plane['prefix']}nosuchrole") is False
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP ROLE IF EXISTS {role}"))
        admin.dispose()


def test_same_name_identified_edge_exists_in_two_workspaces(dp_session_factory, two_workspaces):
    from analystos.artifacts.registry import lineage_for, link
    from analystos.db.models import LineageEdge

    a, b = two_workspaces["a"]["ws"], two_workspaces["b"]["ws"]
    edge = (("dataset", "ds_shared_name"), "built_from", ("table", "src_shared.incident"))
    with dp_session_factory() as s:
        for ws in (a, b, a, b):  # the repeats are idempotent within a workspace
            link(s, ws, edge[0], edge[1], edge[2])
        s.commit()
    with dp_session_factory() as s:
        rows = s.execute(select(LineageEdge.workspace_id, func.count()).where(LineageEdge.from_id == "ds_shared_name")
                         .group_by(LineageEdge.workspace_id)).all()
        assert dict(rows) == {a: 1, b: 1}
        assert lineage_for(s, b, ("table", "src_shared.incident"))["edges"] == [
            {"from": ["dataset", "ds_shared_name"], "relation": "built_from", "to": ["table", "src_shared.incident"]}]


def test_graph_neighbourhood_never_crosses_workspaces(dp_session_factory, two_workspaces, monkeypatch):
    from analystos.artifacts.registry import link
    from analystos.core.config import get_settings
    from analystos.graph import projection

    monkeypatch.setenv("ANALYSTOS_GRAPH_ENABLED", "true")  # the projection is off by default (P4-S03)
    get_settings.cache_clear()
    a, b = two_workspaces["a"]["ws"], two_workspaces["b"]["ws"]
    table = f"src_shared_{new_id('t')[-6:]}.incident"
    with dp_session_factory() as s:
        link(s, a, ("dataset", "ds_from_a"), "built_from", ("table", table))
        link(s, b, ("dataset", "ds_from_b"), "built_from", ("table", table))
        s.commit()
    with dp_session_factory() as s:  # the Postgres neighbourhood (served when the graph is off) is workspace-pinned too
        assert {r["id"] for r in projection.pg_neighborhood(s, [table], a)} == {"ds_from_a"}
        assert {r["id"] for r in projection.pg_neighborhood(s, [table], b)} == {"ds_from_b"}
    try:
        with dp_session_factory() as s:
            ra = projection.project_workspace(s, a)
            if not ra.get("ok"):
                pytest.skip(f"Neo4j not reachable: {ra.get('error')}")
            assert projection.project_workspace(s, b)["ok"]
        near_a = projection.neighborhood([table], a)
        near_b = projection.neighborhood([table], b)
        assert {r["id"] for r in near_a} == {"ds_from_a"}
        assert {r["id"] for r in near_b} == {"ds_from_b"}
        with projection._driver().session() as g:
            nodes = g.run("MATCH (n:AOS {type:'table', id:$t}) RETURN n.workspace_id AS ws", t=table).data()
        assert sorted(r["ws"] for r in nodes) == sorted([a, b])  # one node per workspace, neither overwritten
    finally:
        try:
            with projection._driver().session() as g:
                g.run("MATCH (n:AOS) WHERE n.workspace_id IN $ws DETACH DELETE n", ws=[a, b])
        except Exception:  # noqa: BLE001 - Neo4j down: nothing was written
            pass
        get_settings.cache_clear()


def test_migration_0007_puts_workspace_in_the_lineage_key(dp_control_url):
    """Hand-written migration: upgrade from 0005 swaps the key, downgrade restores it (dedup first)."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from analystos.core.config import REPO_ROOT

    name = f"{make_url(dp_control_url).database}_mig"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")  # noqa: F405
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(dp_control_url).set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0007")

        def key_columns() -> list[list[str]]:
            with engine.connect() as c:
                return [list(r[0]) for r in c.execute(text(
                    "SELECT array_agg(a.attname::text ORDER BY k.ord) FROM pg_constraint con "
                    "CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
                    "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
                    "WHERE con.conrelid = 'lineage_edge'::regclass AND con.contype = 'u' GROUP BY con.oid"))]

        assert key_columns() == [["workspace_id", "from_type", "from_id", "relation", "to_type", "to_id"]]
        with engine.begin() as c:
            for ws in ("ws_a", "ws_b"):
                c.execute(text("INSERT INTO lineage_edge (workspace_id, from_type, from_id, relation, to_type, to_id) "
                               "VALUES (:w, 'dataset', 'd', 'built_from', 'table', 's.t')"), {"w": ws})
        command.downgrade(cfg, "0005")
        assert key_columns() == [["from_type", "from_id", "relation", "to_type", "to_id"]]
        command.upgrade(cfg, "0007")
        assert len(key_columns()[0]) == 6
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()
