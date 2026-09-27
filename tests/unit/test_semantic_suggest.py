"""Data-model suggestion (semantic/suggest.py) on the in-memory control plane: keys, time columns, measures,
relationships, candidate metrics, star schemas and issues from what the catalog knows; measured validation of
keys and join fan-out through a fake gateway over DuckDB (bounded, persisted); the proposal goes through the
structure-review and relationship-candidate paths, never approved by the proposer."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest
from sqlalchemy import select
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.db.base import session_scope
from analystos.db.models import Relationship, SemanticModel, SourceAsset, SourceColumn
from analystos.semantic import suggest as sug

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skills_fixtures import DuckRunSQL  # noqa: E402

ROLES = {("ast_orders", "id"): "identifier", ("ast_orders", "state"): "dimension", ("ast_orders", "amount"): "amount",
         ("ast_orders", "ordered_at"): "timestamp", ("ast_orders", "late"): "flag", ("ast_lines", "id"): "identifier",
         ("ast_lines", "order_id"): "foreign_key", ("ast_lines", "qty"): "measure"}


@pytest.fixture
def model_world(world, sqlite_db):  # noqa: F811
    from analystos.db import models

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in (
        "semantic_relationship_candidate",)])
    with session_scope() as s:
        s.get(SourceAsset, "ast_orders").semantics = {"role": "fact", "entity": "order", "grain": "one row per order",
                                                      "confidence": 0.8}
        s.get(SourceAsset, "ast_lines").semantics = {"role": "fact", "entity": "order line",
                                                     "grain": "one row per order line", "confidence": 0.7}
        for c in s.scalars(select(SourceColumn)):
            c.semantics = {"semantic_role": ROLES[(c.asset_id, c.name)],
                           **({"unit": "currency"} if c.name == "amount" else {}),
                           **({"unit": "count"} if c.name == "qty" else {})}
        s.add(Relationship(id="rel_lines_orders", workspace_id=WS, from_asset_id="ast_lines", from_column="order_id",
                           to_asset_id="ast_orders", to_column="id", cardinality="many_to_one", confidence=0.95,
                           validated=False, evidence={"declared": "orders.id"}, origin="declared"))
    return world


@pytest.fixture
def duck(monkeypatch):
    con = duckdb.connect()
    con.execute("CREATE SCHEMA sales")
    con.execute("CREATE TABLE sales.orders AS SELECT i AS id, CASE WHEN i % 2 = 0 THEN 'Open' ELSE 'Closed' END AS state, "
                "i * 1.5 AS amount, TIMESTAMP '2026-01-01' + to_days(i) AS ordered_at, i % 3 = 0 AS late "
                "FROM range(1, 101) t(i)")
    con.execute("CREATE TABLE sales.order_line AS SELECT i AS id, 1 + (i % 100) AS order_id, i % 5 AS qty "
                "FROM range(1, 401) t(i)")
    runner = DuckRunSQL(con)

    class Gateway:
        def run_sql_for(self, scope, *, actor, source_id=None, **_):  # noqa: ANN001
            return runner

    monkeypatch.setattr("analystos.runtime.context.default_gateway", lambda: Gateway())
    return runner


def _suggest():
    with session_scope() as s:
        return sug.suggest(s, WS)


def test_suggestion_reads_keys_time_measures_relationships_and_metrics(model_world):
    doc = _suggest()
    t = {x["fq"]: x for x in doc["tables"]}
    orders, lines = t["sales.orders"], t["sales.order_line"]
    assert orders["primary_key"] == {"columns": ["id"], "evidence": "declared", "unique": None}
    assert orders["time_column"] == "ordered_at" and orders["measures"] == ["amount"]
    assert set(orders["dimensions"]) == {"state", "late"}
    assert lines["measures"] == ["qty"] and "order_id" not in lines["dimensions"]
    assert {"code": "fact_without_time", "message": lines["issues"][0]["message"]} in lines["issues"]
    (rel,) = doc["relationships"]
    assert rel["from"] == {"asset_id": "ast_lines", "fq": "sales.order_line", "columns": ["order_id"]}
    assert rel["to"]["columns"] == ["id"] and rel["status"] == "proposed" and rel["cardinality"] == "many_to_one"
    assert doc["star_schemas"] == [{"fact": "ast_lines", "dimensions": ["ast_orders"]}]
    names = {m["name"]: m for m in doc["metrics"]}
    assert names["sum_amount"]["expression"] == "SUM(amount)" and names["sum_qty"]["table_fq"] == "sales.order_line"
    assert names["count_order"]["expression"] == "COUNT(*)"
    assert doc["summary"] == {"tables": 2, "facts": 2, "dimensions": 0, "relationships_validated": 0,
                              "relationships_pending": 0, "keys_measured": 0}


def test_sensitive_columns_are_neither_measures_nor_dimensions(model_world):
    with session_scope() as s:
        c = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == "ast_orders", SourceColumn.name == "state"))
        c.tags = ["pii"]
    orders = next(t for t in _suggest()["tables"] if t["fq"] == "sales.orders")
    assert "state" not in orders["dimensions"]


def test_validate_measures_keys_and_fan_out_and_persists_them(model_world, duck):
    with session_scope() as s:
        out = sug.validate(s, s.merge(model_world["owner"]), WS)
    assert {t["asset_id"]: (t["rows"], t["distinct_keys"], t["unique"]) for t in out["tables"]} == {
        "ast_orders": (100, 100, True), "ast_lines": (400, 400, True)}
    assert out["joins"] == [{"from": "sales.order_line", "to": "sales.orders", "from_columns": ["order_id"],
                             "to_columns": ["id"], "rows_before": 400, "rows_after": 400, "fans_out": False}]
    assert out["queries"] <= sug.MAX_QUERIES and all(c["purpose"].startswith("semantic.model_check") or
                                                   c["purpose"].startswith("relationships.") for c in duck.calls)
    with session_scope() as s:
        assert s.get(SourceAsset, "ast_orders").stats["key_check"]["unique"] is True
        assert s.get(Relationship, "rel_lines_orders").evidence["fanout_check"]["fans_out"] is False
    doc = _suggest()
    assert doc["summary"]["keys_measured"] == 2
    assert next(t for t in doc["tables"] if t["fq"] == "sales.orders")["primary_key"]["unique"] is True


def test_validate_finds_duplicate_keys_and_fan_out_and_respects_the_budget(model_world, duck):
    duck.con.execute("INSERT INTO sales.orders SELECT * FROM sales.orders WHERE id <= 3")
    with session_scope() as s:
        out = sug.validate(s, s.merge(model_world["owner"]), WS)
    orders = next(t for t in out["tables"] if t["asset_id"] == "ast_orders")
    assert (orders["rows"], orders["distinct_keys"], orders["unique"]) == (103, 100, False)
    assert out["joins"][0]["fans_out"] is True and out["joins"][0]["rows_after"] > 400
    issues = next(t for t in _suggest()["tables"] if t["fq"] == "sales.orders")["issues"]
    assert any(i["code"] == "key_not_unique" for i in issues)
    with session_scope() as s:
        small = sug.validate(s, s.merge(model_world["owner"]), WS, max_queries=2)
    assert small["queries"] == 2 and small["skipped"]


def test_propose_records_a_proposed_structure_and_queues_measured_candidates(model_world, duck):
    with session_scope() as s:
        out = sug.propose(s, s.merge(model_world["owner"]), WS)
    assert out["status"] == "proposed" and out["candidates_queued"] == 1
    with session_scope() as s:
        row = s.scalar(select(SemanticModel).where(SemanticModel.workspace_id == WS, SemanticModel.version == out["model_version"]))
        ds = {d["source"]: d for d in row.datasets}
        assert ds["sales.orders"]["primary_key"] == ["id"] and ds["sales.orders"]["ai_context"]["grain"] == "one row per order"
    doc = _suggest()
    assert doc["relationships"][0]["status"] == "pending" and doc["relationships"][0]["candidate_id"]
    with session_scope() as s:  # proposing the same structure again changes nothing
        again = sug.propose(s, s.merge(model_world["owner"]), WS)
    assert again["status"] == "unchanged" and again["candidates_queued"] == 0


def _join_fanout(user):
    from analystos.contracts.brief import ReadinessIn
    from analystos.services import readiness

    with session_scope() as s:
        out = readiness.evaluate(s, s.merge(user), WS, ReadinessIn(job_kind="compare",
                                                                    assets=["sales.orders", "sales.order_line"]))
    return next(c for c in out.checks if c.check == "join_fanout")


def _decide(cid: str, *, accepted: bool) -> None:
    """The decision's effect on the relationship table (the hash-bound approval path itself runs in
    tests/integration/test_model_suggestion.py: SQLite returns naive expiry timestamps)."""
    from analystos.core.ids import utcnow
    from analystos.db.models import SemanticRelationshipCandidate
    from analystos.semantic import review

    with session_scope() as s:
        row = s.get(SemanticRelationshipCandidate, cid)
        row.status, row.decided_by, row.decided_at = ("accepted" if accepted else "rejected"), "usr_approver", utcnow()
        review.sync_legacy_relationship(s, row, accepted=accepted)


def test_accepting_a_candidate_validates_the_join_readiness_reads(model_world, duck):
    from analystos.semantic import review

    assert _join_fanout(model_world["owner"]).status == "needs_input"  # declared, not validated on the data
    with session_scope() as s:
        cand = review.propose_candidate(s, s.merge(model_world["owner"]), WS, from_asset="sales.order_line",
                                        from_columns=["order_id"], to_asset="sales.orders", to_columns=["id"])
        cid = cand.id
    assert cand.assessment["approvable"]
    _decide(cid, accepted=True)
    with session_scope() as s:
        rel = s.get(Relationship, "rel_lines_orders")
        assert rel.validated and rel.origin == "review" and rel.cardinality == "many_to_one"
        assert rel.evidence["candidate_id"] == cid and "sql" not in rel.evidence
    check = _join_fanout(model_world["owner"])
    assert check.status == "pass" and "validated" in check.reason
    # the same measurement is not queued again once accepted
    with session_scope() as s:
        again = review.propose_candidate(s, s.merge(model_world["owner"]), WS, from_asset="sales.order_line",
                                         from_columns=["order_id"], to_asset="sales.orders", to_columns=["id"])
        assert again.id == cid and again.status == "accepted"


def test_rejecting_marks_the_join_unvalidated_and_is_not_requeued(model_world, duck):
    from analystos.semantic import review

    with session_scope() as s:
        s.get(Relationship, "rel_lines_orders").validated = True
        cid = review.propose_candidate(s, s.merge(model_world["owner"]), WS, from_asset="sales.order_line",
                                       from_columns=["order_id"], to_asset="sales.orders", to_columns=["id"]).id
    _decide(cid, accepted=False)
    with session_scope() as s:
        rel = s.get(Relationship, "rel_lines_orders")
        assert rel.validated is False and rel.evidence["rejected"]["candidate_id"] == cid
        same = review.propose_candidate(s, s.merge(model_world["owner"]), WS, from_asset="sales.order_line",
                                        from_columns=["order_id"], to_asset="sales.orders", to_columns=["id"])
        assert same.id == cid and same.status == "rejected"  # same measurement: the decision stands
    duck.con.execute("INSERT INTO sales.order_line VALUES (401, 5, 1)")  # a new measurement is new evidence
    with session_scope() as s:
        fresh = review.propose_candidate(s, s.merge(model_world["owner"]), WS, from_asset="sales.order_line",
                                         from_columns=["order_id"], to_asset="sales.orders", to_columns=["id"])
        assert fresh.id != cid and fresh.status == "pending"


def test_an_unjoined_table_says_why_and_a_table_from_another_source_points_at_a_recipe(model_world):
    with session_scope() as s:
        s.delete(s.get(Relationship, "rel_lines_orders"))
    issues = [i for i in _suggest()["issues"] if i["code"] == "orphan_table"]
    assert sorted(i["message"] for i in issues) == ["Order Line joins no other selected table", "Orders joins no other selected table"] \
        or all(i["message"].endswith("joins no other selected table") for i in issues)
    with session_scope() as s:
        s.get(SourceAsset, "ast_lines").source_id = "src_elsewhere"
    msgs = {i["asset_id"]: i["message"] for i in _suggest()["issues"] if i["code"] == "orphan_table"}
    assert "only selected table from its source" in msgs["ast_lines"] and "Prepare data" in msgs["ast_lines"]
