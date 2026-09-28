"""Shared world for the P4-04 / P7-04 / P7-05 / P7-12 unit tests: an in-memory control plane with a
workspace, members of every role, a staged source with profiled tables, and a fake gateway runtime that
answers SQL from fixtures (no service, no data plane)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn, User, Workspace, WorkspaceMember
from analystos.gateway.types import QueryResult

WS = "ws_steps"
EXTRA_TABLES = ("relationship", "recipe", "recipe_run", "ask_thread", "ask_turn", "experiment", "context_entry",
                "workspace_brief", "readiness_assessment", "step_branch", "analysis_step", "analysis_step_version",
                "step_pin", "notebook", "knowledge_pack", "knowledge_document")


class FakeRuntime:
    """Answers SQL from `answers` (first matching substring wins), records every statement it ran."""

    dialect = "postgres"

    def __init__(self, answers: list[tuple[str, list[str], list[list[Any]]]], assets: list[str] | None = None):
        self.answers = answers
        self.asset_sources = {"sales.orders": "src_sales", "sales.order_line": "src_sales", "sn.incident": "src_sales"}
        self.assets = assets or ["sales.orders"]
        self.ran: list[str] = []
        self.python_calls: list[dict[str, Any]] = []

    def sql(self, sql: str, *, source_id: str | None, step_id: str) -> QueryResult:
        from analystos.core.errors import SQLRejected

        self.ran.append(sql)
        for needle, columns, rows in self.answers:
            if needle in sql:
                return QueryResult(query_id=new_id("qry"), columns=columns, rows=rows, row_count=len(rows),
                                   fingerprint=stable_hash(sql), result_hash=stable_hash(rows), referenced_assets=self.assets,
                                   sql=sql)
        raise SQLRejected(f"no such table in the fixture for: {sql}")

    def semantic(self, query, *, step_id):  # noqa: ANN001
        raise NotImplementedError

    def analysis(self, spec, *, step_id):  # noqa: ANN001
        return {"stat": {"n": 120, "statistic": 4.2, "p_value": 0.01, "supported": True}, "table": {"columns": [], "rows": []},
                "query_ids": [new_id("qry")], "sql": ["SELECT 1"], "asset": spec["asset"]}

    def python(self, code: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """A deterministic stand-in: `result` is the sum of every numeric cell of every input."""
        self.python_calls.append({"code": code, "inputs": inputs})
        total = sum(v for k, rows in inputs.items() if k.startswith("cell") for r in rows for v in r.values()
                    if isinstance(v, int | float) and not isinstance(v, bool))
        return {"ok": True, "result": {"total": total}, "isolation": "fake", "duration_ms": 1}


def user(uid: str) -> User:
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
    return u


@pytest.fixture
def world(sqlite_db):
    from analystos.db import models

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in EXTRA_TABLES])
    with session_scope() as s:
        for uid in ("usr_owner", "usr_analyst", "usr_approver", "usr_viewer"):
            s.add(User(id=uid, email=f"{uid}@x", name=uid, password_hash="x", is_admin=False, active=True, attributes={}))
        s.flush()
        s.add(Workspace(id=WS, name="steps", description="", objective="Why are orders late?", autonomy_level=3,
                        status="active", settings={}, policy_version=1, created_by="usr_owner"))
        s.flush()
        for uid, role in (("usr_owner", "owner"), ("usr_analyst", "analyst"), ("usr_approver", "approver"),
                          ("usr_viewer", "viewer")):
            s.add(WorkspaceMember(workspace_id=WS, user_id=uid, role=role))
        s.add(Source(id="src_sales", workspace_id=WS, kind="postgres", name="sales", config={}, status="ready",
                     execution_mode="pushdown"))
        s.flush()
        fresh = utcnow() - timedelta(hours=2)
        s.add(SourceAsset(id="ast_orders", source_id="src_sales", workspace_id=WS, schema_name="sales", name="orders",
                          source_name="orders", kind="table", selected=True, row_count=100, freshness_at=fresh,
                          semantics={"grain": "one row per order", "entity": "order", "confidence": 0.85},
                          stats={"row_count": 100}))
        s.add(SourceAsset(id="ast_lines", source_id="src_sales", workspace_id=WS, schema_name="sales", name="order_line",
                          source_name="order_line", kind="table", selected=True, row_count=400, freshness_at=fresh,
                          semantics={"grain": "one row per order line", "entity": "order line", "confidence": 0.8},
                          stats={"row_count": 400}))
        s.flush()
        cols = [("ast_orders", "id", "integer", True, {"distinct": 100, "null_count": 0, "null_rate": 0.0}),
                ("ast_orders", "state", "text", False, {"distinct": 2, "null_rate": 0.0,
                                                         "top_values": [{"value": "Closed", "count": 60},
                                                                        {"value": "Open", "count": 40}]}),
                ("ast_orders", "amount", "numeric", False, {"null_rate": 0.05}),
                ("ast_orders", "ordered_at", "timestamp", False, {"null_rate": 0.0, "min": "2026-01-01T00:00:00",
                                                                  "max": "2026-09-01T00:00:00"}),
                ("ast_orders", "late", "boolean", False, {"null_rate": 0.1, "distinct": 2}),
                ("ast_lines", "id", "integer", True, {"distinct": 400, "null_count": 0}),
                ("ast_lines", "order_id", "integer", False, {"distinct": 100, "null_count": 0}),
                ("ast_lines", "qty", "integer", False, {"null_rate": 0.0})]
        for i, (aid, name, dtype, key, prof) in enumerate(cols):
            s.add(SourceColumn(asset_id=aid, name=name, ordinal=i, data_type=dtype, is_key=key, tags=[], profile=prof,
                               semantic_type="datetime" if dtype == "timestamp" else None))
    return {u: user(f"usr_{u}") for u in ("owner", "analyst", "approver", "viewer")}
