"""Playbook engine (P4-X02): `investigate.v1` reproduces the v1 plan, hash and replan semantics, and
the engine and dispatch hold no step keys of their own."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from analystos.capabilities import registry
from analystos.capabilities.agents import entry_for
from analystos.capabilities.playbook import Playbook, PlaybookBody, evaluate, parse, parse_condition
from analystos.contracts.capability import CapabilityManifest
from analystos.runtime.plan import base_plan, plan_hash

OBJECTIVE = "Find the drivers of SLA breaches in IT incidents"
FRAMING = {"questions": ["Which groups breach SLA most?", "Is backlog trending?"], "audience": ["executive"], "focus": ["priority"]}
FILTERS = {"filters": [{"column": "assignment_group", "op": "=", "value": "Network"}]}

# Computed with the v1 code (hardcoded BASE_STEPS, commit 120d2ab) before the playbook engine existed:
# (autonomy_level, framed) -> (hash with no constraints, scope-abc, v1; hash with FILTERS, scope-abc, v2).
V1_HASHES = {
    (3, False): ("ede2ee5c3a75de8ec14f3f7460ba20609ac6fdf72459d31d7261d057fef87409",
                 "ae8daccb8da466e0eea43df5259f01bf6e2b99d28b74bc8b77032b80bc1c511e"),
    (3, True): ("55ecfbaa0f85e1d585621d170607d435195634432eb6ee0af850f145988bf154",
                "69becec02dc33302b1d94a573b3104cb45ccee15e109ca720a9bdc55fe54ae86"),
    (2, False): ("07e076344eb9c9b6c685c4e94cb0a33936ed500adc8660da9e36d97d5c97cda4",
                 "0cc7dce83bfe33f167d593ee5286c9b62cf2646cb886458cf1022271cb564b6e"),
    (2, True): ("dc630f2efe4b060fa47d8dbb5fb901db4e9154791e05bcd4e4d7ad830633a897",
                "92706dbff45d07909d3668578f4e92bd0f52b499da32691f24e0767c0f81342f"),
}
# The v1 engine constants the playbook replaced.
V1_REPLAN_RESET = {"hypotheses", "insights", "verify", "dataset", "semantic", "visualize", "publish_request", "publish", "finalize"}
V1_DOWNSTREAM_OF_VERIFY = {"dataset", "semantic", "visualize", "publish_request", "publish", "finalize"}


@pytest.fixture(scope="module")
def investigate() -> Playbook:
    return parse(registry.load(packs_dir=None, entry_points=False).get("playbook.investigate"))


@pytest.mark.parametrize(("level", "framed"), sorted(V1_HASHES))
def test_investigate_v1_reproduces_the_v1_plan_hash(investigate, level, framed):
    kw = FRAMING if framed else {}
    plan = investigate.build_plan(OBJECTIVE, autonomy_level=level, **kw)
    first, redirected = V1_HASHES[(level, framed)]
    assert plan_hash(plan, constraints={}, scope_hash="scope-abc", plan_version=1) == first
    assert plan_hash(plan, constraints=FILTERS, scope_hash="scope-abc", plan_version=2) == redirected
    assert base_plan(OBJECTIVE, autonomy_level=level, **kw) == plan  # the default playbook is investigate


def test_investigate_v1_replan_and_expansion_semantics_match_v1(investigate):
    assert investigate.reset_keys("redirect") == V1_REPLAN_RESET
    assert investigate.reset_keys("finding_rejected") == V1_DOWNSTREAM_OF_VERIFY
    assert investigate.dynamic_prefixes == ("test:", "followups:")
    assert investigate.removed_prefixes("redirect") == ("test:", "followups:")
    assert investigate.removed_prefixes("finding_rejected") == ()
    assert investigate.is_dynamic("test:H-3") and not investigate.is_dynamic("insights")
    waits = {k for k, s in investigate.steps.items() if s.waits_for_approval}
    assert waits == {"plan_approval", "publish"}
    assert {k for k, s in investigate.steps.items() if s.type == "side_effect"} == {"publish"}
    skip = {"run": {"origin": {"publish": "skip"}}}
    assert {k for k, s in investigate.steps.items() if s.skip_when and evaluate(s.skip_when.if_, skip)} == {"publish_request", "publish"}
    assert not evaluate(investigate.steps["publish"].skip_when.if_, {"run": {"origin": {"type": "user"}}})


def test_dispatch_resolves_every_v1_task_to_the_v1_behaviour(investigate):
    """The literal v1 dispatch map, now derived from the playbook and the agent manifests."""
    from analystos.agents import (
        context_agent,
        critic,
        data_scientist,
        insight,
        investigator,
        metadata,
        profiler,
        publisher,
        semantic,
        sql_agent,
        supervisor,
        visualization,
    )

    v1 = {"plan_approval": supervisor.plan_approved, "context": context_agent.load_context, "metadata": metadata.collect_metadata,
          "relationships": metadata.discover_relationships, "profile": profiler.profile_tables, "quality": profiler.check_quality,
          "hypotheses": investigator.generate_hypotheses, "insights": insight.build_insights, "verify": critic.verify_insights,
          "dataset": sql_agent.build_dataset, "semantic": semantic.define_metrics, "visualize": visualization.design,
          "publish_request": publisher.request_publication, "publish": publisher.publish, "finalize": supervisor.finalize,
          "test:H-1": data_scientist.test_hypothesis, "followups:2": investigator.follow_ups}
    snap = registry.current()
    for key, fn in v1.items():
        step = investigate.step(key) or investigate.expansion(key)
        assert registry.resolve_python(entry_for(snap.get(step.use), step.behaviour)) is fn, key


def test_engine_and_dispatch_hold_no_step_keys():
    src = Path(__file__).resolve().parents[2] / "src" / "analystos"
    for path in (src / "runtime" / "engine.py", src / "agents" / "dispatch.py", src / "runtime" / "plan.py"):
        text = path.read_text()
        for key in ("plan_approval", "publish_request", "publish", "hypotheses", "verify", "dataset", "finalize",
                    "test:", "followups:"):
            assert not re.search(rf"[\"']{re.escape(key)}[\"']", text), f"{path.name} still names step {key!r}"


def test_plan_hash_binds_capability_versions():
    plan = {"steps": []}
    legacy = plan_hash(plan, constraints={}, scope_hash="s", plan_version=1)
    assert plan_hash(plan, constraints={}, scope_hash="s", plan_version=1, capabilities=[]) == legacy
    v1 = plan_hash(plan, constraints={}, scope_hash="s", plan_version=1, capabilities=["agent.x@1.0.0", "playbook.p@1.0.0"])
    v2 = plan_hash(plan, constraints={}, scope_hash="s", plan_version=1, capabilities=["agent.x@1.1.0", "playbook.p@1.0.0"])
    assert len({legacy, v1, v2}) == 3
    assert v1 == plan_hash(plan, constraints={}, scope_hash="s", plan_version=1, capabilities=["playbook.p@1.0.0", "agent.x@1.0.0"])


def _pb(steps):
    return {"steps": steps}


@pytest.mark.parametrize(("steps", "error"), [
    ([{"key": "a", "use": "agent.x", "title": "A"}, {"key": "a", "use": "agent.x", "title": "A"}], "duplicate step keys"),
    ([{"key": "a", "use": "agent.x", "title": "A", "after": ["b"]}], "unknown step b"),
    ([{"key": "a", "use": "agent.x", "title": "A", "after": ["b"]}, {"key": "b", "use": "agent.x", "title": "B"}], "declared later"),
    ([{"key": "g", "use": "agent.x", "title": "G", "type": "approval_gate", "payload": "bundle", "approval_for": "a"},
      {"key": "a", "use": "agent.x", "title": "A"}], "approval_for must name a side_effect step"),
    ([{"key": "g", "use": "agent.x", "title": "G", "type": "approval_gate"}], "needs `payload`"),
    ([{"key": "a", "use": "agent.x", "title": "A", "when": "__import__('os')"}], "must be '<path> <op> <literal>'"),
    ([{"key": "a", "use": "agent.x", "title": "A", "decorative": True}], "Extra inputs are not permitted"),
])
def test_playbook_structure_is_validated(steps, error):
    with pytest.raises(ValidationError, match=re.escape(error)):
        PlaybookBody.model_validate(_pb(steps))


def test_conditions_are_single_comparisons():
    ns = {"run": {"autonomy_level": 2, "origin": {"publish": "skip"}}}
    assert evaluate("run.autonomy_level <= 2", ns) and not evaluate("run.autonomy_level > 2", ns)
    assert evaluate("run.origin.publish == 'skip'", ns) and evaluate("run.origin.missing != 'x'", ns)
    assert not evaluate("run.origin.missing < 3", ns)
    assert parse_condition("run.x == true") == ("run.x", "==", True)
    with pytest.raises(ValueError):
        parse_condition("run.x == [1, 2]")


def test_a_pack_playbook_needs_registered_agents_and_behaviours(tmp_path):
    d = tmp_path / "packs" / "demo"
    d.mkdir(parents=True)
    (d / "pb.yaml").write_text("""apiVersion: analystos/v1
kind: Playbook
id: playbook.demo
summary: demo
spec:
  steps:
    - {key: a, use: agent.nobody, title: A}
    - {key: b, use: agent.metadata, behaviour: no_such_behaviour, title: B}
""")
    with pytest.raises(registry.CapabilityLoadError) as err:
        registry.load(packs_dir=tmp_path / "packs", entry_points=False)
    assert "step a uses agent.nobody, which is not a registered agent" in str(err.value)
    assert "step b: agent agent.metadata has no behaviour no_such_behaviour" in str(err.value)
    m = CapabilityManifest.model_validate({"kind": "Playbook", "id": "playbook.x", "summary": "x",
                                           "spec": {"steps": [{"key": "a", "use": "agent.context", "title": "A"}]}})
    assert Playbook(m).build_plan("objective text", autonomy_level=3)["steps"] == [
        {"key": "a", "agent": "context", "title": "A", "depends_on": [], "optional": False}]
