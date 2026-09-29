"""N-11 on the compose Postgres: queries run through the gateway become audited history; the advisor reads
that history, asks the staged source for dry plans through `QueryGateway.explain` (reader identity, read-only),
and stores index advice with its evidence. Nothing is applied: the staged table keeps its indexes, and the
advice DDL sent to the gateway is rejected and audited. Skips cleanly when the stack is down."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.errors import SQLRejected  # noqa: E402
from analystos.db.models import AuditEvent, IndexAdvice, LineageEdge, QueryExecution, RunEvent  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration

SLOW = ("SELECT number, short_description FROM incident WHERE priority = 1 AND category = 'Network' "
        "ORDER BY number")


def _index_count(settings, schema: str) -> int:
    engine = create_engine(settings.analytics_loader_url)
    try:
        with engine.connect() as c:
            return c.execute(text("SELECT COUNT(*) FROM pg_indexes WHERE schemaname = :s AND tablename = 'incident'"),
                             {"s": schema}).scalar_one()
    finally:
        engine.dispose()


def test_advice_from_gateway_history_and_plans_is_stored_never_applied(
        dp_settings, dp_session_factory, servicenow_scope, staged_servicenow, dp_workspace) -> None:
    from analystos.gateway.service import QueryGateway
    from analystos.services import index_advice as svc

    gw = QueryGateway(dp_settings, session_factory=dp_session_factory)
    for _ in range(3):
        gw.execute(servicenow_scope, SLOW, actor="user:test", purpose="console", use_cache=False)
    schema = staged_servicenow["schema"]
    before = _index_count(dp_settings, schema)

    with dp_session_factory() as s:
        out = svc.advise(s, servicenow_scope, actor="user:test", gateway=gw, min_ms=0, min_table_rows=0)
        s.commit()
    assert out["slow_fingerprints"] >= 1 and out["explained"] >= 1, out
    incident = [i for i in out["items"] if i["asset"] == f"{schema}.incident"]
    assert incident, out
    top = incident[0]
    assert top["kind"] == "index" and set(top["columns"]) <= {"priority", "category"} and top["status"] == "open"
    assert top["evidence"]["queries"][0]["executions"] == 3
    # the dry plan through the gateway corroborates it: a sequential scan filtering on the column
    nodes = top["evidence"]["plan_nodes"]
    assert nodes and nodes[0]["node"] in ("Seq Scan", "Parallel Seq Scan"), top["evidence"]
    assert "Network" not in repr(top["evidence"])  # plan literals never kept
    assert "Staged copy" in " ".join(top["notes"]) and top["applied_by_platform"] is False

    with dp_session_factory() as s:
        explains = list(s.scalars(select(QueryExecution).where(QueryExecution.purpose == "index_advice.explain")))
        assert explains and {e.status for e in explains} == {"explained"}
        row = s.get(IndexAdvice, top["id"])
        assert row.ddl.startswith("-- Advisory only")
        edges = list(s.scalars(select(LineageEdge).where(LineageEdge.from_type == "index_advice",
                                                         LineageEdge.from_id == row.id)))
        assert {e.to_type for e in edges} == {"query"}
        assert s.scalar(select(RunEvent).where(RunEvent.type == "index_advice.generated",
                                               RunEvent.workspace_id == dp_workspace["workspace_id"])) is not None
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "index_advice.analyzed")) is not None

    # Nothing was applied, and the advice itself cannot be run through the gateway.
    assert _index_count(dp_settings, schema) == before
    with pytest.raises(SQLRejected):
        gw.execute(servicenow_scope, row.ddl, actor="user:test", purpose="console", use_cache=False)
    with pytest.raises(SQLRejected):
        gw.execute(servicenow_scope, row.ddl.split("\n")[-1], actor="user:test", purpose="console", use_cache=False)

    # A person's dismissal survives re-analysis; the row is updated in place, not duplicated.
    with dp_session_factory() as s:
        svc.review(s, s.merge(_user(s, dp_workspace)), s.get(IndexAdvice, top["id"]), "dismissed", "not now")
        s.commit()
    with dp_session_factory() as s:
        again = svc.advise(s, servicenow_scope, actor="user:test", gateway=gw, min_ms=0, min_table_rows=0)
        s.commit()
    same = [i for i in again["items"] if i["id"] == top["id"]]
    assert same and same[0]["status"] == "dismissed"
    with dp_session_factory() as s:
        keys = [a.advice_key for a in s.scalars(select(IndexAdvice).where(
            IndexAdvice.workspace_id == dp_workspace["workspace_id"]))]
        assert len(keys) == len(set(keys))


def _user(s, dp_workspace):
    from analystos.db.models import User

    return s.get(User, dp_workspace["user_id"])
