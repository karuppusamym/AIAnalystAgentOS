"""P6-08: the `engineer` declarative agent proposes recipe nodes and PipelineSpecs only; every proposal passes
the recipe IR validator (and the scope and PipelineSpec checks) before it could be compiled; the `off` mode
takes the rules path; `prepare.v1` is a published built-in playbook. Model proposals come from
tests/fakes.py FakeTransport; the control plane is SQLite; no services."""
from __future__ import annotations

import copy

import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.agents import generic
from analystos.capabilities import registry
from analystos.capabilities.agents import to_agent_spec
from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.contracts.policy import DataScope, WorkspacePolicyDoc
from analystos.db.base import session_scope
from analystos.db.models import (
    AnalysisRun,
    RunTask,
    Source,
    SourceAsset,
    SourceColumn,
    ToolDefinition,
    User,
    Workspace,
    WorkspaceCapability,
    WorkspaceMember,
    WorkspacePolicy,
)
from analystos.llm.router import ModelRouter
from analystos.runtime.context import RunContext, Services
from analystos.tools.registry import BUILTIN_TOOLS

ASSET = "src_1.incident"
COLUMNS = [("sys_id", "text", True, []), ("priority", "text", False, []), ("caller_email", "text", False, ["pii"]),
           ("reassignment_count", "bigint", False, []), ("sys_updated_on", "timestamp without time zone", False, []),
           ("_raw", "text", False, [])]


@pytest.fixture
def world(sqlite_db, monkeypatch):
    from analystos.services import platform_settings

    state = {"settings": PlatformSettings()}
    monkeypatch.setattr(platform_settings, "get", lambda: state["settings"])
    registry.reload(entry_points=False)
    with session_scope() as s:
        s.add(User(id="usr_1", email="a@x", name="A", password_hash="-", active=True, attributes={}))
        s.add(Workspace(id="ws_1", name="W", autonomy_level=2, created_by="usr_1", policy_version=1, settings={}))
        s.add(WorkspaceMember(workspace_id="ws_1", user_id="usr_1", role="editor"))
        s.add(WorkspacePolicy(workspace_id="ws_1", version=1, document={}, created_by="usr_1"))
        s.add(WorkspaceCapability(workspace_id="ws_1", capability_id="agent.engineer", enabled=True, updated_by="usr_1"))
        for t in BUILTIN_TOOLS:
            s.add(ToolDefinition(id=t.tool_id, spec=t.model_dump(), enabled=True))
        s.add(Source(id="src_1", workspace_id="ws_1", kind="servicenow", name="SN", config={}, status="ready"))
        s.add(SourceAsset(id="ast_1", source_id="src_1", workspace_id="ws_1", schema_name="src_1", name="incident",
                          source_name="incident", selected=True, row_count=1200, stats={}))
        for i, (name, dtype, key, tags) in enumerate(COLUMNS):
            s.add(SourceColumn(asset_id="ast_1", name=name, ordinal=i, data_type=dtype, is_key=key, tags=tags, profile={},
                               semantics={}))
        s.add(AnalysisRun(id="run_1", workspace_id="ws_1", objective="Prepare the incident table for analysis",
                          status="RUNNING", autonomy_level=2, plan={"steps": []}, plan_version=1, scope={"hash": "h"},
                          instructions=[], constraints={}, control="run", requested_by="usr_1", summary={}, origin={},
                          capabilities={}))
        s.add(RunTask(id="tsk_1", run_id="run_1", key="proposal", agent_id="engineer", title="p", status="RUNNING",
                      depends_on=[], input={}, output={}, plan_version=1, seq=0))
    yield state
    registry.reload(entry_points=False)


def _agent():
    return registry.current().get("agent.engineer").model_copy()


def _ctx(proposals: list | None = None):
    manifest = _agent()
    calls = iter(proposals or [])
    transport = FakeTransport(chat=lambda payload: chat_json(next(calls)))
    router = ModelRouter(transport=transport, api_key_lookup=lambda env: "k")
    with session_scope() as s:
        run, task = s.get(AnalysisRun, "run_1"), s.get(RunTask, "tsk_1")
        user, ws = s.get(User, "usr_1"), s.get(Workspace, "ws_1")
        s.expunge_all()
    scope = DataScope(workspace_id="ws_1", user_id="usr_1", role="editor", source_ids=["src_1"], assets=[ASSET],
                      asset_sources={ASSET: "src_1"}, columns={ASSET: [c[0] for c in COLUMNS]})
    ctx = RunContext(run=run, task=task, user=user, workspace=ws, policy=WorkspacePolicyDoc(), scope=scope,
                     agent=to_agent_spec(manifest), services=Services(router=router, gateway=None), manifest=manifest)
    return ctx, transport, manifest


def _modes(world, mode: str):
    world["settings"] = PlatformSettings(llm=LLMSettings(purpose_modes={"pipeline_proposal": mode}, cache_enabled=False))


def test_manifests_load_and_prepare_is_a_published_builtin():
    from analystos.capabilities.validation import validate_kinds
    from analystos.services.definitions import builtin_ref

    registry.reload(entry_points=False)
    snap = registry.current()
    for cid in ("agent.engineer", "skill.pipeline_propose", "playbook.prepare"):
        assert cid in snap.manifests, cid
    problems = [p for p in validate_kinds(snap.manifests) if any(k in p for k in ("engineer", "pipeline_propose", "prepare"))]
    assert problems == []
    assert builtin_ref(snap.manifests["playbook.prepare"]).status == "published"
    assert [s["use"] for s in snap.manifests["playbook.prepare"].spec["steps"]] == ["agent.metadata", "agent.engineer"]


def test_off_mode_takes_the_rules_path_and_the_proposal_is_validated(world):
    _modes(world, "off")
    ctx, transport, agent = _ctx()
    skipped: list[str] = []
    ctx.router.record_skip = lambda purpose, *a, **k: skipped.append(purpose)
    out = generic.run_agent(ctx, agent.model_copy(update={"spec": {**agent.spec, "output": None}}))
    assert transport.chat_calls == [] and out["mode"] == "deterministic" and ctx.usage["llm_calls"] == 0
    assert "pipeline_proposal" in skipped  # the avoided model call is recorded as a saving
    result = out["results"][0]["result"]
    proposal = result["proposals"][0]
    assert result["valid"] == 1 and proposal["valid"] and proposal["source"] == "rules" and proposal["compiled"] is False
    recipe, pipeline = proposal["recipe"], proposal["pipeline"]
    assert [n["op"] for n in recipe["nodes"]] == ["source", "dedupe", "output"]
    cols = [c["name"] for c in recipe["nodes"][0]["schema"]]
    # PII (the agent's pii_access: none) and names outside the IR are left out, with reasons
    assert "caller_email" not in cols and "_raw" not in cols and cols[0] == "sys_id"
    assert {s["column"] for s in proposal["skipped_columns"]} == {"caller_email", "_raw"}
    assert recipe["nodes"][1]["order"] == [{"column": "sys_updated_on", "desc": True}]
    assert pipeline["incremental"]["watermark"] == "sys_updated_on" and pipeline["incremental"]["key"] == ["sys_id"]
    assert pipeline["output"]["keys"] == ["sys_id"] and proposal["recipe_hash"]


def test_model_proposals_are_rejected_until_the_ir_validator_accepts_them(world):
    _modes(world, "always")
    ctx, _, agent = _ctx()
    good = copy.deepcopy(generic_rule_recipe(ctx))
    bad = copy.deepcopy(good)
    bad["nodes"].insert(1, {"op": "filter", "id": "urgent", "input": "src", "predicate": "priority = 1"})  # text vs number
    bad["nodes"][2]["input"] = "urgent"
    outside = copy.deepcopy(good)
    outside["nodes"][0]["asset"] = "hr.salaries"
    rounds = [
        {"actions": [{"capability": "skill.pipeline_propose", "input": {"recipe": bad}},
                     {"capability": "skill.pipeline_propose", "input": {"recipe": outside}}], "done": False},
        {"actions": [{"capability": "skill.pipeline_propose", "input": {"recipe": good}}], "done": True,
         "summary": "One valid staging recipe."},
    ]
    ctx, transport, agent = _ctx(rounds)
    out = generic.run_agent(ctx, agent.model_copy(update={"spec": {**agent.spec, "output": None}}))
    assert out["mode"] == "model" and len(transport.chat_calls) == 2
    first, second, third = (r["result"]["proposals"][0] for r in out["results"])
    assert not first["valid"] and any("compares text with numeric" in p for p in first["problems"])
    assert not second["valid"] and any("not in the authorized scope" in p for p in second["problems"])
    assert third["valid"] and third["compiled"] is False
    # the rejection reached the model in the next round's history
    assert "compares text with numeric" in transport.chat_calls[1]["messages"][1]["content"]


def test_a_pipeline_whose_contract_differs_from_the_recipe_is_rejected(world):
    from analystos.skills.pipeline_propose import pipeline_propose

    ctx, _, _ = _ctx()
    good = generic_rule_recipe(ctx)
    from analystos.skills.pipeline_propose import rule_proposal

    pipeline = copy.deepcopy(rule_proposal(ctx, ASSET)["pipeline"])
    pipeline["output"]["keys"] = ["priority"]
    pipeline["joins"] = [{"node": "src", "expected_cardinality": "one_to_one"}]
    out = pipeline_propose(ctx, recipe=good, pipeline=pipeline)["proposals"][0]
    assert not out["valid"]
    assert any("output.keys" in p for p in out["problems"]) and any("src is not a join" in p for p in out["problems"])


def generic_rule_recipe(ctx) -> dict:
    from analystos.skills.pipeline_propose import rule_proposal

    return rule_proposal(ctx, ASSET)["recipe"]
