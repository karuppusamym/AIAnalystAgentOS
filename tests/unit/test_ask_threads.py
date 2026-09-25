"""P4-U02 Ask (no services): the ask_route / clarify_needed decisions in the Ask flow (rules first,
the deterministic path unchanged), streamed stages, one refusal per kind, promotion helpers, the
gateway plan summary, and the generic capability invoke checks."""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from analystos.agents import sql_agent
from analystos.contracts.capability import CapabilityManifest
from analystos.core.errors import (
    BudgetExceeded,
    InvalidInput,
    PolicyDenied,
    QueryTimeout,
    SQLRejected,
    UpstreamUnavailable,
)
from analystos.decisions.service import DecisionService
from analystos.decisions.store import MemoryDecisionStore
from analystos.services import ask as ask_svc


class Result:
    query_id, columns, rows, row_count, truncated = "qry_1", ["group", "n"], [["Network", 3]], 1, False
    referenced_assets, cache_hit, result_hash, duration_ms = ["stg.incident"], False, "h", 4


class Gateway:
    def __init__(self):
        self.calls: list[str] = []

    def execute(self, scope, sql, **kw):
        self.calls.append(sql)
        return Result()


def _ctx(router=None, decisions=None):
    stages: list[tuple[str, str]] = []
    services = SimpleNamespace(router=router, gateway=Gateway(), decisions=decisions)
    ctx = SimpleNamespace(user=SimpleNamespace(id="usr_1"), workspace=SimpleNamespace(id="ws_1"),
                          scope=SimpleNamespace(source_dialects={"src": "postgres"}, assets=["stg.incident"]), policy=None,
                          agent=SimpleNamespace(id="sql"), services=services, run=None, task=None,
                          on_stage=lambda key, text, data: stages.append((key, text)))
    return ctx, stages


@pytest.fixture
def no_gate(monkeypatch):
    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "catalog_for_prompt", lambda ctx, **kw: "catalog")
    monkeypatch.setattr(sql_agent, "compile_for", lambda ctx, purpose, required, **kw: required)
    monkeypatch.setattr(sql_agent, "session_scope", lambda: contextlib.nullcontext(SimpleNamespace(get=lambda *a: None)))


def _hit(score=1.8, missing=()):
    entry = SimpleNamespace(id="vq_1", name="p1_by_group", sql_template="SELECT 1", parameters=[], dialect="postgres")
    return SimpleNamespace(entry=entry, values={}, missing=[{"name": m} for m in missing], score=score, pattern="p1 by group")


# ------------------------------------------------------------------------------------ routing
def test_every_registry_match_scores_as_a_verified_match_so_the_rule_keeps_routing_it():
    assert sql_agent.verified_match_score(1.4) == 0.8
    assert sql_agent.verified_match_score(2.0) == 1.0
    assert 0.8 < sql_agent.verified_match_score(1.7) < 1.0


def test_a_registry_hit_is_routed_to_the_verified_query_by_the_rule_with_no_model(no_gate, monkeypatch):
    monkeypatch.setattr(sql_agent, "_registry_match", lambda ctx, q, p: _hit())
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("no model call on a registry hit"))
    ctx, stages = _ctx()
    out = sql_agent.ask(ctx, "P1 by group")
    assert out["status"] == "answered" and out["answered_by"] == "registry" and out["route"] == "verified_query"
    assert out["decisions"][0]["purpose"] == "ask_route" and out["decisions"][0]["backend"] == "rules"
    assert [k for k, _ in stages] == ["scope", "registry", "route", "registry", "execute"]
    assert out["result"]["referenced_assets"] == ["stg.incident"]


def test_a_missing_required_input_declines_before_any_model(no_gate, monkeypatch):
    monkeypatch.setattr(sql_agent, "_registry_match", lambda ctx, q, p: _hit(missing=["priority"]))
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("no model call when an input is missing"))
    ctx, _ = _ctx()
    out = sql_agent.ask(ctx, "P1 by group")
    assert out["status"] == "needs_input" and out["route"] == "decline" and out["missing"] == [{"name": "priority"}]
    assert ctx.services.gateway.calls == []


def test_a_miss_generates_and_the_sql_still_goes_through_the_gateway(no_gate, monkeypatch):
    monkeypatch.setattr(sql_agent, "_registry_match", lambda ctx, q, p: None)
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: ({"sql": "SELECT g, COUNT(*) FROM stg.incident GROUP BY 1"}, "m"))
    ctx, stages = _ctx()
    out = sql_agent.ask(ctx, "How many incidents per group?")
    assert out["answered_by"] == "model" and out["route"] == "generate"
    assert [d["purpose"] for d in out["decisions"]] == ["ask_route", "clarify_needed"]
    assert out["decisions"][1]["value"] == "answer"
    assert ctx.services.gateway.calls == ["SELECT g, COUNT(*) FROM stg.incident GROUP BY 1"]
    assert "Finding the tables that answer this" in [t for _, t in stages]


def test_a_question_that_names_nothing_to_measure_is_clarified_not_generated(no_gate, monkeypatch):
    monkeypatch.setattr(sql_agent, "_registry_match", lambda ctx, q, p: None)
    monkeypatch.setattr(sql_agent, "llm_json", lambda *a, **k: pytest.fail("no generation for an ambiguous question"))
    ctx, _ = _ctx()
    out = sql_agent.ask(ctx, "what about it?")
    assert out["status"] == "clarify" and out["missing"] == [{"name": "what to measure"}]


def test_decisions_are_recorded_against_the_turn_when_a_router_is_present(no_gate, monkeypatch):
    from tests.unit.test_decision_service import router

    store = MemoryDecisionStore()
    r = router()
    monkeypatch.setattr(sql_agent, "_registry_match", lambda ctx, q, p: _hit())
    ctx, _ = _ctx(router=r, decisions=DecisionService(r, store=store))
    seen = {}
    ctx.subject = "ask:askt_1"
    ctx.call_ctx = lambda: seen.setdefault("ctx", SimpleNamespace(workspace_id="ws_1", task_id="askt_1", run_id=None,
                                                                   agent_id="sql", allowed_models=[], exclude_families=[]))
    monkeypatch.setattr(sql_agent, "_record_skip", lambda *a: None)
    out = sql_agent.ask(ctx, "P1 by group")
    assert out["route"] == "verified_query"
    assert [(d.purpose, d.value, d.subject) for d in store.decisions] == [("ask_route", "verified_query", "ask:askt_1")]


# ------------------------------------------------------------------------------------ refusals
@pytest.mark.parametrize("exc,kind", [
    (SQLRejected("not read-only"), "sql_rejected"),
    (BudgetExceeded("over"), "budget_exceeded"),
    (PolicyDenied("denied"), "policy_denied"),
    (QueryTimeout("slow"), "timeout"),
    (UpstreamUnavailable("down"), "unavailable"),
    (InvalidInput("SQL generation unavailable (no model route) — write SQL"), "no_model"),
    (InvalidInput("something else"), "failed"),
])
def test_each_failure_is_one_refusal_kind_with_a_remedy(exc, kind):
    r = ask_svc.refusal_for(exc)
    assert r["kind"] == kind and r["remedy"] and r["title"] and r["message"] == exc.message


def test_every_refusal_kind_has_a_title_and_a_remedy():
    assert all(v["title"] and v["remedy"] for v in ask_svc.REFUSALS.values())
    status, r = ask_svc._finish({"status": "needs_input", "missing": [{"name": "priority"}], "explanation": "x"})
    assert status == "needs_input" and r["kind"] == "needs_input" and r["details"]["missing"] == [{"name": "priority"}]
    assert ask_svc._finish({"status": "answered"}) == ("answered", None)


# ------------------------------------------------------------------------------------ streaming
def test_stream_turn_sends_stages_as_they_happen_then_the_turn_and_end():
    def fake_ask(user, thread_id, question, parameters, *, on_stage):
        on_stage({"key": "scope", "text": "Checking what you are allowed to see"})
        on_stage({"key": "done", "text": "Answered"})
        return {"id": "askt_1", "status": "answered"}

    async def collect():
        return [f async for f in ask_svc.stream_turn(SimpleNamespace(id="u"), "ask_1", "q", ask=fake_ask)]

    frames = asyncio.run(collect())
    events = [f.split("\n", 1)[0] for f in frames]
    assert events == ["event: stage", "event: stage", "event: turn", "event: end"]
    assert '"askt_1"' in frames[2]


def test_stream_turn_reports_an_error_event():
    def failing(user, thread_id, question, parameters, *, on_stage):
        raise InvalidInput("ask a question")

    async def collect():
        return [f async for f in ask_svc.stream_turn(SimpleNamespace(id="u"), "ask_1", "", ask=failing)]

    frames = asyncio.run(collect())
    assert frames[0].startswith("event: error") and "invalid_input" in frames[0] and frames[-1].startswith("event: end")


# ------------------------------------------------------------------------------------ promotion helpers
def test_measure_of_takes_the_first_aggregate_unqualified():
    assert ask_svc.measure_of("SELECT t.g, COUNT(*) AS n FROM s.t t GROUP BY 1") == "COUNT(*)"
    assert ask_svc.measure_of("SELECT g, AVG(t.hours) FROM s.t t GROUP BY g") == "AVG(hours)"
    assert ask_svc.measure_of("SELECT g FROM s.t") is None
    assert ask_svc.measure_of("not sql at all (") is None


def test_staleness_is_unknown_without_recorded_freshness():
    turn = SimpleNamespace(status="answered", provenance={"assets": []})
    assert ask_svc.staleness(None, turn)["state"] == "unknown"
    refused = SimpleNamespace(status="refused", provenance={})
    assert ask_svc.staleness(None, refused)["label"] == "Data freshness unknown"


def test_staleness_reads_age_and_data_refreshed_after_the_answer():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 25, 12, tzinfo=UTC)

    class Session:
        def __init__(self, current):
            self.current = current

        def scalars(self, stmt):
            return [SimpleNamespace(id="ast_1", freshness_at=self.current)]

    def turn(recorded):
        return SimpleNamespace(status="answered", provenance={"assets": [{"asset_id": "ast_1", "freshness_at": recorded.isoformat()}]})

    fresh = now - timedelta(hours=2)
    assert ask_svc.staleness(Session(fresh), turn(fresh), now)["state"] == "fresh"
    old = now - timedelta(days=9)
    assert ask_svc.staleness(Session(old), turn(old), now)["state"] == "stale"
    assert ask_svc.staleness(Session(now), turn(old), now)["state"] == "changed"


# ------------------------------------------------------------------------------------ gateway explain
def test_plan_summary_walks_every_node_and_relation():
    from analystos.gateway.service import plan_summary

    plan = [{"Plan": {"Node Type": "Aggregate", "Plan Rows": 5, "Total Cost": 12.5, "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "incident", "Schema": "stg", "Plan Rows": 4210}]}}]
    out = plan_summary(plan)
    assert out == {"node": "Aggregate", "estimated_rows": 5, "total_cost": 12.5, "nodes": ["Aggregate", "Seq Scan"],
                   "relations": ["stg.incident"]}


# ------------------------------------------------------------------------------------ capability invoke
def _manifest(**kw):
    base = {"kind": "Skill", "id": "skill.demo", "summary": "demo", "entry": "python:analystos.skills.lookup:row_count",
            "side_effect": "read_source", "spec": {"call": "context"},
            "input_schema": {"type": "object", "required": ["asset"], "properties": {"asset": {"type": "string"}},
                             "additionalProperties": False}}
    return CapabilityManifest.model_validate({**base, **kw})


def test_invoke_input_is_validated_against_the_manifest_schema():
    from analystos.capabilities.invoke import schema_errors

    m = _manifest()
    assert schema_errors(m, {"asset": "stg.incident"}) == []
    errs = schema_errors(m, {"other": 1})
    assert errs and all({"loc", "msg"} <= set(e) for e in errs)


def test_only_context_entries_are_invocable_and_the_payload_binds_the_version():
    from analystos.capabilities.invoke import executable, payload_for

    assert executable(_manifest())
    assert not executable(_manifest(kind="Method", id="method.demo", spec={}))
    assert not executable(_manifest(entry="builtin:sql.execute"))
    assert payload_for(_manifest(), {"asset": "a"}) == {"capability": "skill.demo@1.0.0", "arguments": {"asset": "a"}}


def test_invoke_never_runs_a_side_effect_without_an_approval(monkeypatch):
    """A write capability returns an approval request; the entry is not called."""
    from analystos.capabilities import invoke as inv

    m = _manifest(side_effect="write_external")
    calls = []

    class Snap:
        def get(self, cid):
            return m

    class S:
        def merge(self, u):
            return u

        def expunge_all(self):
            pass

        def get(self, *a, **k):
            return None

    class Scope:
        def __enter__(self):
            return S()

        def __exit__(self, *a):
            return False

    apr = SimpleNamespace(id="apr_1", payload_hash="ph")
    monkeypatch.setattr(inv, "session_scope", Scope)
    monkeypatch.setattr(inv, "_snapshot", lambda s, ws: Snap())
    monkeypatch.setattr(inv.enablement, "usable", lambda *a, **k: None)
    monkeypatch.setattr(inv.enablement, "overrides", lambda *a, **k: {})
    monkeypatch.setattr("analystos.governance.policy.require_role", lambda *a, **k: "owner")
    monkeypatch.setattr("analystos.governance.policy.get_workspace", lambda s, ws: SimpleNamespace(id=ws, policy_version=1))
    monkeypatch.setattr("analystos.governance.approvals.request_approval", lambda s, **kw: calls.append(kw) or apr)
    monkeypatch.setattr(inv.registry, "resolve_entry", lambda m: pytest.fail("a side effect must not run before approval"))
    out = inv.invoke(SimpleNamespace(id="usr_1"), "ws_1", "skill.demo", {"asset": "stg.incident"})
    assert out["status"] == "approval_required" and out["approval_id"] == "apr_1"
    assert calls[0]["action"] == "capability.invoke" and calls[0]["payload"]["capability"] == "skill.demo@1.0.0"
    assert calls[0]["risk_tier"] == "high"


def test_capability_list_rows_carry_ui_hints_and_input_schema():
    from analystos.api.routers.capabilities import _out

    m = _manifest(ui={"form": "auto", "renderer": "renderer.table"})
    out = _out(m, None)
    assert out["ui"] == {"form": "auto", "renderer": "renderer.table"} and out["input_schema"]["required"] == ["asset"]
