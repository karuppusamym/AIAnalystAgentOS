"""P4-S01 unit checks: step -> queue routing, queue config, worker queue selection (no services)."""
from __future__ import annotations

import pytest

from analystos.workflows import queues

# The investigate lifecycle's step keys (runtime/plan.py BASE_STEPS today, the investigate.v1 playbook after P4-X02).
STEP_KEYS = ["plan_approval", "context", "metadata", "relationships", "profile", "quality", "hypotheses", "insights",
             "verify", "dataset", "semantic", "visualize", "publish_request", "publish", "finalize"]


def test_every_plan_step_routes_to_a_known_workload():
    routed = {key: queues.workload_for_step(key) for key in STEP_KEYS}
    assert set(routed.values()) <= set(queues.WORKLOADS)
    assert routed["profile"] == routed["quality"] == "compute"
    assert routed["publish_request"] == routed["publish"] == "publish"
    assert routed["context"] == routed["insights"] == routed["verify"] == routed["finalize"] == "analysis"
    assert queues.workload_for_step("test:h_123") == "compute"
    assert queues.workload_for_step("followups:1") == "analysis"
    assert queues.workload_for_step("plan_approval") == "analysis"


def test_repo_config_loads_and_keeps_engine_tasks_within_the_claim_ttl():
    from analystos.runtime.engine import CLAIM_TTL_SECONDS

    specs, max_activities = queues.load_config()
    assert set(specs) == set(queues.WORKLOADS)
    assert specs["compute"].executor == "process"
    assert max_activities > 0
    for workload in ("analysis", "compute", "publish"):  # execute_task runs here; a retry must be able to retake the claim
        assert specs[workload].start_to_close_seconds <= CLAIM_TTL_SECONDS
    assert all(s.heartbeat_seconds < s.start_to_close_seconds for s in specs.values())


def test_config_rejects_unknown_workloads_and_bad_timeouts(tmp_path):
    bad = tmp_path / "q.yaml"
    bad.write_text("queues:\n  gpu: {max_concurrent: 1}\n")
    with pytest.raises(ValueError, match="unknown workload"):
        queues.load_config(bad)
    bad.write_text("queues:\n  compute: {heartbeat_seconds: 900, start_to_close_seconds: 600}\n")
    with pytest.raises(ValueError, match="heartbeat_seconds"):
        queues.load_config(bad)


def test_workflow_options_name_queues_by_prefix():
    opts = queues.workflow_options("acme")
    assert opts["queues"]["compute"]["name"] == "acme-compute"
    assert opts["queues"]["analysis"]["name"] == "acme-analysis"
    fallback = queues.fallback_options("acme")
    assert fallback["queues"].keys() == opts["queues"].keys()
    assert all({"name", "start_to_close", "heartbeat"} <= set(q) for q in fallback["queues"].values())


def test_parse_workloads():
    assert queues.parse_workloads(None) == list(queues.WORKLOADS)
    assert queues.parse_workloads("all") == list(queues.WORKLOADS)
    assert queues.parse_workloads("analysis, compute,analysis") == ["analysis", "compute"]
    with pytest.raises(ValueError, match="unknown queue"):
        queues.parse_workloads("analysis,gpu")


def test_worker_cli_accepts_queues(monkeypatch):
    from analystos import cli

    seen = {}
    monkeypatch.setattr("analystos.workflows.worker.run_worker", lambda q=None: seen.setdefault("queues", q))
    cli.main(["worker", "--queues", "compute"])
    assert seen["queues"] == "compute"


def test_heartbeat_pump_is_a_no_op_outside_an_activity():
    from analystos.workflows.activities import heartbeat_pump, heartbeating

    with heartbeat_pump():
        pass
    assert heartbeating(lambda a, b: a + b, 1, 2) == 3


def test_graph_projection_is_off_by_default(monkeypatch):
    from analystos.core.config import get_settings
    from analystos.graph import projection

    monkeypatch.delenv("ANALYSTOS_GRAPH_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().graph_enabled is False
        result = projection.project_workspace(None, "ws_x")  # disabled: never touches the session or Neo4j
        assert result["skipped"] is True and result["ok"] is False
    finally:
        get_settings.cache_clear()
