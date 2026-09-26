from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from analystos.core.errors import InvalidInput
from analystos.core.ids import stable_hash
from analystos.db.models import QueryExecution, SemanticMetric, SemanticModel
from analystos.semantic import evidence
from analystos.services import ask, saved_analysis


@pytest.fixture
def world(monkeypatch):
    from analystos.contracts.policy import DataScope, WorkspacePolicyDoc
    from analystos.semantic import service

    policy = WorkspacePolicyDoc()
    model = NS(id="m", status="approved", content_hash="mh", workspace_id="w")
    metric = NS(id="metric", status="approved", content_hash="kh", workspace_id="w")
    receipt = NS(status="ok", workspace_id="w", result_hash="rh")
    semantic = {"model_id": "m", "model_hash": "mh", "model_version": 1, "sql_hash": stable_hash("SELECT amount FROM sales.orders"),
                "policy_hash": stable_hash(policy.model_dump(mode="json")),
                "metrics": [{"name": "revenue", "id": "metric", "version": 1, "hash": "kh"}]}
    turn = NS(id="old", thread_id="thread", workspace_id="w", status="answered", question="revenue", sql="SELECT amount FROM sales.orders",
              result={"query_id": "q", "result_hash": "rh"}, provenance={"semantic": semantic})
    rows = {SemanticModel: model, SemanticMetric: metric, QueryExecution: receipt}
    session = NS(get=lambda cls, ident: rows.get(cls), merge=lambda user: user)
    monkeypatch.setattr(service, "current_model", lambda *args: model)
    monkeypatch.setattr(evidence, "get_workspace", lambda *args: NS(id="w"))
    monkeypatch.setattr(evidence, "load_policy", lambda *args: policy)
    scope = DataScope(workspace_id="w", user_id="u", role="analyst")
    return NS(session=session, turn=turn, rows=rows, policy=policy, scope=scope, model=model, metric=metric)


def test_recorded_is_not_a_statistical_verification(world):
    result = evidence.evidence_status(world.session, world.turn, {"state": "unknown", "label": "Unknown"})
    assert result["state"] == "recorded"
    assert "not an independent statistical verification" in result["reasons"][0]


@pytest.mark.parametrize("changed", ["sql", "model", "metric", "policy", "receipt", "data", "workspace"])
def test_changed_or_missing_dependency_removes_current_status(world, changed):
    freshness = {"state": "fresh", "label": "Fresh"}
    if changed == "sql":
        world.turn.sql += " LIMIT 2"
    elif changed == "model":
        world.model.content_hash = "new"
    elif changed == "metric":
        world.metric.status = "deprecated"
    elif changed == "policy":
        world.policy.max_rows = 1
    elif changed == "receipt":
        world.rows[QueryExecution] = None
    elif changed == "data":
        freshness["state"] = "changed"
    else:
        world.metric.workspace_id = "another-workspace"
    assert evidence.evidence_status(world.session, world.turn, freshness)["state"] == "changed"


def test_refresh_pins_sql_and_edit_drops_governed_claim(world, monkeypatch):
    from analystos.agents import sql_agent
    from analystos.artifacts import registry

    @contextmanager
    def session_scope():
        yield world.session

    calls = []
    ctx = NS(scope=world.scope, policy=world.policy, turn_id="new", semantic_catalog={"id": "m", "hash": "mh",
             "metrics": {"revenue": {"id": "metric", "hash": "kh"}}})

    def execute(scope, sql, **kw):
        calls.append((sql, kw))
        return NS(query_id="newq", columns=["amount"], rows=[[42]], row_count=1, truncated=False)

    ctx.services = NS(gateway=NS(execute=execute))
    monkeypatch.setattr(ask, "session_scope", session_scope)
    monkeypatch.setattr(ask, "_turn_for", lambda *a, **kw: world.turn)
    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda c: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda c: None)
    monkeypatch.setattr(registry, "link", lambda *a, **kw: None)
    monkeypatch.setattr(ask, "ask_in_thread", lambda user, thread, question, ask_fn: {
        **ask_fn(ctx, question), "id": "new", "workspace_id": "w"})
    snapshot = deepcopy(vars(world.turn))
    refreshed = ask.rerun_turn(NS(id="u"), "old")
    assert refreshed["governance"] == "governed"
    assert calls[0][0] == world.turn.sql and calls[0][1]["use_cache"] is False
    edited = ask.rerun_turn(NS(id="u"), "old", "SELECT amount * 2 FROM sales.orders")
    assert edited["governance"] == "ad_hoc" and edited["semantic"] is None
    assert edited["parent_turn_id"] == "old"
    assert vars(world.turn) == snapshot
    ctx.semantic_catalog["metrics"]["revenue"]["hash"] = "changed"
    with pytest.raises(InvalidInput, match="pinned metric changed"):
        ask.rerun_turn(NS(id="u"), "old")
    assert len(calls) == 2


def test_schedule_pin_and_approval_are_both_checked(world, monkeypatch):
    monkeypatch.setattr(ask, "_turn_for", lambda *a, **kw: world.turn)
    checked = []

    def verify(session, approval_id, **kw):
        checked.append(kw["payload"])
        return NS(action=saved_analysis.ACTION, workspace_id="w", requested_by="u")

    monkeypatch.setattr(saved_analysis, "verify_for_execution", verify)
    config = {"turn_id": "old", "fingerprint": saved_analysis.fingerprint(world.turn), "approval_id": "a"}
    saved_analysis.verify(world.session, NS(id="u"), "w", "Daily revenue", "0 9 * * *", "UTC", config)
    assert checked[0]["cron"] == "0 9 * * *"
    world.turn.sql += " LIMIT 1"
    with pytest.raises(InvalidInput, match="no longer matches"):
        saved_analysis.verify(world.session, NS(id="u"), "w", "Daily revenue", "0 9 * * *", "UTC", config)
    assert len(checked) == 1


def test_schedule_deltas_and_no_change():
    before = {"columns": ["revenue"], "rows": [[10]], "result_hash": "a"}
    assert saved_analysis.changes(before, before)["changed"] is False
    after = {**before, "rows": [[14]], "result_hash": "b"}
    assert saved_analysis.changes(before, after)["deltas"][0]["change"] == 4
    assert saved_analysis.changes(before, {**after, "rows": [[14], [16]]})["deltas"] == []
