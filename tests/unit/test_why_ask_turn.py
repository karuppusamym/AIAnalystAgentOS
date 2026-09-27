"""P7-08 for Ask answers: every number of an Ask turn resolves fact -> step -> query receipt -> data version ->
semantic version -> verdict, each link with its current state; a broken or voided link is shown, never hidden."""
from __future__ import annotations

from datetime import timedelta

import pytest

from analystos.core.errors import NotFound
from analystos.core.ids import utcnow
from analystos.evidence import verification as V
from analystos.evidence.why import LINKS, explain_ask_turn

WS = "ws_why"


@pytest.fixture
def db(sqlite_db):
    from analystos.db import models

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in (
        "ask_thread", "ask_turn", "step_branch", "analysis_step", "analysis_step_version")
        if t in models.Base.metadata.tables])
    return sqlite_db


def _turn(s, *, governed: bool = True):
    from analystos.db.models import (
        AskThread,
        AskTurn,
        QueryExecution,
        SemanticMetric,
        SemanticModel,
        Source,
        SourceAsset,
        User,
        Workspace,
    )

    fresh = utcnow() - timedelta(hours=2)
    s.add(User(id="u1", email="u1@why.test", name="U", password_hash="x"))
    s.add(Workspace(id=WS, name="why", created_by="u1", policy_version=0))
    s.add(Source(id="src1", workspace_id=WS, kind="postgres", name="shop", execution_mode="staged"))
    s.add(SourceAsset(id="a1", source_id="src1", workspace_id=WS, schema_name="shop", name="orders", source_name="orders",
                      row_count=10, freshness_at=fresh))
    s.add(QueryExecution(id="q1", workspace_id=WS, source_id="src1", actor="user:u1", sql="SELECT region, SUM(x) FROM o",
                         status="ok", result_hash="rh1", row_count=2))
    semantic = None
    if governed:
        s.add(SemanticModel(id="sm1", workspace_id=WS, name="m", version=1, status="approved", origin="user", content_hash="mh",
                            created_by="u1"))
        s.add(SemanticMetric(id="met1", workspace_id=WS, name="revenue", version=1, status="approved", definition={},
                             expression="SUM(x)", normalized_expression="sum(x)", proposed_by="u1", proposed_via="user",
                             content_hash="c1"))
        semantic = {"model_id": "sm1", "model_version": 1, "model_hash": "mh", "compiler_version": "sem.v1",
                    "metrics": [{"id": "met1", "name": "revenue", "version": 1, "hash": "c1"}]}
    s.add(AskThread(id="th1", workspace_id=WS, user_id="u1", title="t"))
    turn = AskTurn(id="turn1", thread_id="th1", workspace_id=WS, user_id="u1", question="revenue by region?",
                   status="answered", sql="SELECT region, SUM(x) FROM o",
                   result={"query_id": "q1", "result_hash": "rh1", "columns": ["region", "revenue"],
                           "rows": [["north", 12.5], ["south", 4]], "row_count": 2},
                   provenance={"governance": "governed" if governed else "ad_hoc", "semantic": semantic,
                               "assets": [{"asset": "shop.orders", "asset_id": "a1", "source_id": "src1",
                                           "freshness_at": fresh.isoformat()}]})
    s.add(turn)
    s.flush()
    return turn


def test_every_number_of_an_answer_resolves_through_six_links(db):
    from analystos.db.models import AskTurn

    with db() as s:
        out = explain_ask_turn(s, _turn(s))
        assert [n["value"] for n in out["numbers"]] == [12.5, 4] and out["numbers_total"] == 2
        n = out["numbers"][0]
        assert [lk["link"] for lk in n["links"]] == list(LINKS)
        states = {lk["link"]: lk["state"] for lk in n["links"]}
        assert states == {"fact": "ok", "step": "not_applicable", "query_receipt": "ok", "data_version": "ok",
                          "semantic_version": "ok", "verdict": "unknown"}
        assert n["links"][0]["detail"]["labels"] == {"region": "north"} and n["links"][0]["detail"]["column"] == "revenue"
        assert "ingest" in n["links"][5]["reason"]  # the missing verdict says how to get one
        assert out["subject"]["type"] == "ask_turn" and out["subject"]["governance"] == "governed"
        only = explain_ask_turn(s, s.get(AskTurn, "turn1"), number="4")
        assert [x["row"] for x in only["numbers"]] == [1]
        with pytest.raises(NotFound):
            explain_ask_turn(s, s.get(AskTurn, "turn1"), number="99")


def test_a_changed_or_broken_link_and_a_void_verdict_are_shown(db):
    from analystos.db.models import AnalysisStep, QueryExecution, SemanticMetric, SourceAsset, StepBranch

    with db() as s:
        turn = _turn(s)
        s.add(StepBranch(id="br1", workspace_id=WS, container_type="ask_thread", container_id="th1", name="main",
                          created_by="user:u1"))
        s.add(AnalysisStep(id="stp1", workspace_id=WS, branch_id="br1", container_type="ask_thread", container_id="th1",
                           kind="query", title="q", origin={"type": "ask_turn", "id": "turn1"}, status="ok",
                           created_by="user:u1"))
        s.flush()
        rec = V.record_verdict(s, workspace_id=WS, run_id=None, subject_type="step", subject_id="stp1", verdict="verified",
                               checks=[], verifier="selfcheck.v1", dependencies=[V.Dependency("query", "step:stp1", "v1")])
        V.void_dependents(s, "query", "step:stp1", "v2", "step edited (version 2)", event="step.edited")
        s.get(QueryExecution, "q1").result_hash = "tampered"
        s.get(SourceAsset, "a1").freshness_at = utcnow()
        s.get(SemanticMetric, "met1").status = "deprecated"
        s.flush()
        out = explain_ask_turn(s, turn, column="revenue", row=0)
        [n] = out["numbers"]
        states = {lk["link"]: lk["state"] for lk in n["links"]}
        assert states == {"fact": "ok", "step": "ok", "query_receipt": "broken", "data_version": "changed",
                          "semantic_version": "changed", "verdict": "void"}
        assert n["state"] == "broken" and out["verification_state"]["record_id"] == rec.id
        assert "result hash differs" in n["links"][2]["reason"] and "refreshed after the answer" in n["links"][3]["reason"]
        assert "metric revenue v1" in n["links"][4]["reason"] and n["links"][5]["reason"].startswith("query: step edited")


def test_an_ad_hoc_answer_has_no_semantic_link_and_an_unanswered_turn_is_refused(db):
    from analystos.db.models import AskTurn

    with db() as s:
        turn = _turn(s, governed=False)
        out = explain_ask_turn(s, turn)
        assert {lk["link"]: lk["state"] for lk in out["numbers"][0]["links"]}["semantic_version"] == "not_applicable"
        s.get(AskTurn, "turn1").status = "refused"
        with pytest.raises(NotFound):
            explain_ask_turn(s, turn)
