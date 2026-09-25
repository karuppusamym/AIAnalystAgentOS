"""Capability platform (P4-X01 remainder): validation of agent manifests at load, entry-point plugins,
per-workspace enablement, certification gating, run bindings in the plan hash, hot reload and runs in
flight keeping their versions. SQLite control plane, no services."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from analystos.capabilities import binding, enablement
from analystos.capabilities import registry as reg
from analystos.core.config import get_settings
from analystos.core.errors import PolicyDenied
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, RunTask, WorkspaceCapability
from analystos.runtime import engine
from analystos.runtime.plan import plan_hash


@pytest.fixture(autouse=True)
def _fresh_registry():
    reg.reload(entry_points=False)
    yield
    reg.reload(entry_points=False)


# ------------------------------------------------------------------------------------ load-time validation
def _pack(tmp_path: Path, body: str, name: str = "demo") -> Path:
    d = tmp_path / "packs" / name
    d.mkdir(parents=True)
    (d / "m.yaml").write_text(body)
    return tmp_path / "packs"


AGENT = """apiVersion: analystos/v1
kind: Agent
id: agent.pack_helper
summary: helper
entry: builtin:generic
spec:
  role: Helper
  goal: Help
  capabilities: [{caps}]
  tools: [{tools}]
  model_purpose: {purpose}
{extra}"""


def _agent(caps="skill.catalog_lookup", tools="metadata.read", purpose="agent_actions", extra=""):
    return AGENT.format(caps=caps, tools=tools, purpose=purpose, extra=extra)


def test_a_pack_agent_with_an_unknown_skill_name_fails_the_load(tmp_path):
    with pytest.raises(reg.CapabilityLoadError, match="agent.pack_helper: unknown capability skill.no_such_skill"):
        reg.load(packs_dir=_pack(tmp_path, _agent(caps="skill.catalog_lookup, skill.no_such_skill")), entry_points=False)
    assert reg.load(packs_dir=_pack(tmp_path / "fixed", _agent()), entry_points=False).get("agent.pack_helper")


@pytest.mark.parametrize(("kw", "error"), [
    ({"tools": "no.such_tool"}, "unknown tool no.such_tool"),
    ({"purpose": "mind_reading"}, "unknown model purpose mind_reading"),
    ({"extra": "  verification_required: true\n"}, "verification_required: Extra inputs are not permitted"),
    ({"extra": "  policies: {approval_for_publish: false}\n"}, "approval_for_publish cannot be disabled"),
    ({"extra": "  default_actions: [{capability: skill.row_count, input: {}}]\n"}, "default action skill.row_count is not one of"),
    ({"extra": "  output: {artifact_type: poster, artifact_name: x}\n"}, "unknown artifact type poster"),
])
def test_every_agent_manifest_field_is_checked_at_load(tmp_path, kw, error):
    with pytest.raises(reg.CapabilityLoadError, match=error):
        reg.load(packs_dir=_pack(tmp_path, _agent(**kw)), entry_points=False)


def test_old_agent_files_are_still_read_and_validated(tmp_path):
    agents = tmp_path / "agents"
    shutil.copytree(get_settings().agents_dir, agents)
    (agents / "legacy.yaml").write_text(yaml.safe_dump({"agent": {"id": "legacy", "name": "Legacy", "description": "old format",
                                                                  "skills": ["dataset_profile"], "tools": ["profile.table"]}}))
    snap = reg.load(packs_dir=None, entry_points=False, agents_dir=agents)
    legacy = snap.get("agent.legacy")
    assert legacy.spec["capabilities"] == ["skill.dataset_profile"] and legacy.certification.status == "certified"
    (agents / "legacy.yaml").write_text(yaml.safe_dump({"agent": {"id": "legacy", "name": "Legacy", "description": "old",
                                                                  "skills": ["root_cause_analysis"]}}))
    with pytest.raises(reg.CapabilityLoadError, match="unknown capability skill.root_cause_analysis"):
        reg.load(packs_dir=None, entry_points=False, agents_dir=agents)


def test_the_repository_catalog_loads_strictly():
    snap = reg.load(entry_points=False)
    assert not snap.problems
    agents = {m.id for m in snap.list("Agent")}
    assert {"agent.investigator", "agent.data_dictionary", "agent.catalog_steward"} <= agents
    assert {m.id for m in snap.list("Playbook")} >= {"playbook.investigate", "playbook.data_dictionary"}


# ------------------------------------------------------------------------------------ entry-point plugins
def _install_plugin(tmp_path: Path) -> Path:
    """What `pip install analystos-demo-plugin` leaves in site-packages: a package and its dist-info."""
    site = tmp_path / "site"
    pkg = site / "analystos_demo_plugin"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "def manifests():\n"
        "    return [{'kind': 'Method', 'id': 'method.cohort_demo', 'version': '0.3.0', 'summary': 'Cohort retention',\n"
        "             'side_effect': 'read_source', 'cost_class': 'query', 'requires': ['skill.run_analysis'],\n"
        "             'certification': {'status': 'tested', 'evidence': 'tests/test_cohort.py'}}]\n")
    dist = site / "analystos_demo_plugin-0.3.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: analystos-demo-plugin\nVersion: 0.3.0\n")
    (dist / "entry_points.txt").write_text("[analystos.capabilities]\ndemo = analystos_demo_plugin:manifests\n")
    (dist / "RECORD").write_text("")
    return site


def test_a_pip_installed_plugin_appears_on_reload_without_core_changes(tmp_path, monkeypatch):
    before = reg.current()
    assert "method.cohort_demo" not in before.manifests
    monkeypatch.syspath_prepend(str(_install_plugin(tmp_path)))
    fresh = reg.reload(packs_dir=None)  # hot reload: no restart, no core code change
    m = fresh.get("method.cohort_demo")
    assert m.source == "entrypoint:analystos-demo-plugin" and m.ref == "method.cohort_demo@0.3.0"
    assert reg.current() is fresh and fresh.digest != before.digest
    assert "method.cohort_demo" not in before.manifests  # a holder of the old snapshot is unaffected
    assert not enablement.is_enabled(m, fresh, {})  # a plugin is disabled by default in every workspace
    assert enablement.usable(m, fresh, {"method.cohort_demo": True}, autonomous_run=True).endswith("use certified capabilities only")
    assert enablement.usable(m, fresh, {"method.cohort_demo": True}, autonomous_run=False) is None


def test_a_bad_reload_keeps_the_good_registry(tmp_path):
    good = reg.current()
    with pytest.raises(reg.CapabilityLoadError):
        reg.reload(packs_dir=_pack(tmp_path, _agent(caps="skill.no_such_skill")), entry_points=False)
    assert reg.current() is good


# ------------------------------------------------------------------------------------ enablement defaults
def test_default_enablement_is_the_investigate_closure_plus_builtin_tools():
    snap = reg.current()
    on = enablement.default_enabled(snap)
    assert {"playbook.investigate", "agent.investigator", "agent.data_scientist", "agent.publisher", "skill.run_analysis",
            "skill.dataset_profile", "tool.sql_execute", "tool.superset_publish", "tool.crawl_run"} <= on
    assert not {"playbook.data_dictionary", "agent.data_dictionary", "agent.catalog_steward", "skill.catalog_lookup",
                "agent.integration"} & on
    assert enablement.is_enabled(snap.get("connector.postgres"), snap, {})  # governed by source registration
    assert enablement.is_enabled(snap.get("agent.data_dictionary"), snap, {"agent.data_dictionary": True})
    assert not enablement.is_enabled(snap.get("agent.investigator"), snap, {"agent.investigator": False})


# ------------------------------------------------------------------------------------ binding
def _run(playbook: str | None = None, autonomy: int = 3, origin: dict | None = None) -> None:
    with session_scope() as s:
        s.add(AnalysisRun(id="run_1", workspace_id="ws_1", objective="Document the selected incident tables", status="NEW",
                          autonomy_level=autonomy, plan={}, plan_version=0, scope={"hash": "h", "assets": ["sn.incident"]},
                          instructions=[], constraints={}, control="run", requested_by="usr_1", summary={},
                          origin=origin or {"type": "user"}, capabilities={"playbook": playbook} if playbook else {}))


def _enable(*ids: str, enabled: bool = True) -> None:
    with session_scope() as s:
        for i in ids:
            s.add(WorkspaceCapability(workspace_id="ws_1", capability_id=i, enabled=enabled, updated_by="usr_1"))


def _bind():
    with session_scope() as s:
        return binding.bind_run(s, s.get(AnalysisRun, "run_1"))


def test_a_playbook_outside_the_default_set_is_disabled_until_enabled(sqlite_db):
    _run("playbook.data_dictionary", autonomy=2)
    with pytest.raises(PolicyDenied, match="playbook.data_dictionary@1.0.0 is disabled for this workspace"):
        _bind()
    _enable("playbook.data_dictionary")  # enabling a playbook enables what it binds, unless disabled explicitly
    b = _bind()
    assert not b.skipped
    assert {"playbook.data_dictionary@1.0.0", "agent.data_dictionary@1.0.0", "agent.metadata@1.0.0",
            "skill.catalog_lookup@1.0.0", "skill.column_profile_lookup@1.0.0", "tool.metadata_read@1.0.0"} <= set(b.refs)
    assert b.manifests["agent.data_dictionary"]["version"] == "1.0.0"
    _enable("agent.data_dictionary", enabled=False)
    b = _bind()  # the optional dictionary step's agent is disabled: the step is skipped, the run is not failed
    assert b.skipped == {"dictionary": "capability agent.data_dictionary@1.0.0 is disabled for this workspace"}


@pytest.mark.parametrize(("autonomy", "origin"), [(3, None), (2, {"type": "schedule"}), (1, {"type": "alert"})])
def test_only_certified_capabilities_run_autonomously(sqlite_db, autonomy, origin):
    _run("playbook.data_dictionary", autonomy=autonomy, origin=origin)
    _enable("playbook.data_dictionary", "agent.data_dictionary")
    with pytest.raises(PolicyDenied, match="is tested, not certified: autonomous runs"):
        _bind()


def test_disabling_an_optional_agent_skips_its_step_and_a_required_one_fails_the_plan(sqlite_db):
    _run()
    _enable("agent.data_quality", enabled=False)
    b = _bind()
    assert b.skipped == {"quality": "capability agent.data_quality@1.0.0 is disabled for this workspace"}
    assert "agent.data_quality@1.0.0" not in b.refs and "agent.profiler@1.0.0" in b.refs
    _enable("agent.sql", enabled=False)
    with pytest.raises(PolicyDenied, match="step dataset: capability agent.sql@1.0.0 is disabled"):
        _bind()


def test_materialized_plan_hash_binds_versions_and_skips_come_from_the_playbook(sqlite_db):
    _run(origin={"type": "schedule", "publish": "skip"})
    _enable("agent.data_quality", enabled=False)
    b = _bind()
    with session_scope() as s:
        run = s.get(AnalysisRun, "run_1")
        run.plan = b.playbook.build_plan(run.objective, autonomy_level=3)
        run.capabilities, run.plan_version = b.to_json(), 1
        engine.materialize_plan(s, run)
        expected = plan_hash(run.plan, constraints={}, scope_hash="h", plan_version=1, capabilities=b.refs)
        assert run.plan_hash == expected != plan_hash(run.plan, constraints={}, scope_hash="h", plan_version=1)
    with session_scope() as s:
        skipped = {t.key: t.error for t in s.scalars(select(RunTask).where(RunTask.status == "SKIPPED"))}
    assert skipped == {"quality": "capability agent.data_quality@1.0.0 is disabled for this workspace",
                       "publish_request": "publication not requested for this run",
                       "publish": "publication not requested for this run"}


def test_a_run_in_flight_keeps_its_bound_versions_across_a_reload(sqlite_db, tmp_path):
    from analystos.runtime.context import _agent

    _run("playbook.data_dictionary", autonomy=2)
    _enable("playbook.data_dictionary", "agent.data_dictionary")
    b = _bind()
    with session_scope() as s:
        run = s.get(AnalysisRun, "run_1")
        run.plan, run.capabilities, run.plan_version = b.playbook.build_plan(run.objective, autonomy_level=2), b.to_json(), 1
        engine.materialize_plan(s, run)
        hash_before = run.plan_hash

    agents = tmp_path / "agents"
    shutil.copytree(get_settings().agents_dir, agents)
    doc = yaml.safe_load((agents / "data_dictionary.yaml").read_text())
    doc["version"], doc["spec"]["budget"] = "1.1.0", {"llm_calls": 9, "queries": 5}
    (agents / "data_dictionary.yaml").write_text(yaml.safe_dump(doc))
    reg.reload(entry_points=False, agents_dir=agents)
    assert reg.current().get("agent.data_dictionary").version == "1.1.0"

    with session_scope() as s:
        run = s.get(AnalysisRun, "run_1")
        assert binding.resolve(run, "agent.data_dictionary").version == "1.0.0"
        manifest, spec = _agent(s, run, "data_dictionary")
        assert manifest.version == "1.0.0" and spec.version == "1.0.0" and manifest.spec["budget"]["llm_calls"] == 3
        assert binding.run_playbook(run).ref == "playbook.data_dictionary@1.0.0"
        engine.apply_replan(s, run, "redirect", full=True)
        assert "agent.data_dictionary@1.0.0" in run.capabilities["refs"] and run.plan_hash != hash_before
        assert run.plan_hash == plan_hash(run.plan, constraints={}, scope_hash="h", plan_version=2, capabilities=b.refs)
        # a new run binds the new version, so its plan hash differs for the same inputs
        fresh = binding.bind_run(s, run)
        assert "agent.data_dictionary@1.1.0" in fresh.refs and "agent.data_dictionary@1.0.0" not in fresh.refs
