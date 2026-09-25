"""Run engine (`runtime/engine.py`) without services: get_state scheduling decisions, execute_task
claim/idempotency/failure/replan handling, apply_replan, and plan-version stamping of artifacts."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from analystos.core.errors import InvalidInput, RunCancelled
from analystos.core.ids import utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, RunEvent, RunTask
from analystos.runtime import engine


def _run(*tasks: tuple, status: str = "READY", control: str = "run", plan_version: int = 1) -> str:
    """tasks: (key, status, depends_on[, input])."""
    with session_scope() as s:
        s.add(AnalysisRun(id="run_1", workspace_id="ws_1", objective="find the drivers of SLA breaches", status=status,
                          plan={"steps": []}, plan_version=plan_version, scope={"hash": "h"}, instructions=[], constraints={},
                          control=control, requested_by="usr_1", summary={}, origin={}))
        for i, (key, st, deps, *rest) in enumerate(tasks):
            s.add(RunTask(id=f"tsk_{i}", run_id="run_1", key=key, agent_id="a", title=key, status=st, depends_on=deps,
                          input=(rest[0] if rest else {}), output={}, plan_version=plan_version, seq=i * 10))
    return "run_1"


def _task(key: str) -> RunTask:
    with session_scope() as s:
        return s.scalar(select(RunTask).where(RunTask.run_id == "run_1", RunTask.key == key))


def _approval(status: str) -> str:
    with session_scope() as s:
        s.add(Approval(id="apr_1", workspace_id="ws_1", run_id="run_1", action="publish_dashboard", risk_tier="high",
                       payload={}, payload_hash="x", policy_version=1, requested_by="usr_1", status=status,
                       expires_at=utcnow() + timedelta(days=1), evidence={}))
    return "apr_1"


# ------------------------------------------------------------------------------------ get_state
def test_get_state_needs_plan_without_tasks(sqlite_db):
    _run()
    assert engine.get_state("run_1") == {"needs_plan": True}


def test_get_state_terminal_and_controls(sqlite_db):
    _run(("a", "NEW", []), status="COMPLETED")
    assert engine.get_state("run_1") == {"terminal": True, "status": "COMPLETED"}
    with session_scope() as s:
        s.get(AnalysisRun, "run_1").status, s.get(AnalysisRun, "run_1").control = "RUNNING", "cancel"
    assert engine.get_state("run_1") == {"control": "cancel"}
    with session_scope() as s:
        s.get(AnalysisRun, "run_1").control = "pause"
    assert engine.get_state("run_1") == {"control": "pause"}
    with session_scope() as s:
        assert s.get(AnalysisRun, "run_1").status == "PAUSED"
        assert s.scalar(select(RunEvent).where(RunEvent.type == "run.status")).payload == {"status": "PAUSED"}


def test_get_state_ready_follows_dependencies_and_seq(sqlite_db):
    _run(("context", "COMPLETED", []), ("metadata", "NEW", ["context"]), ("profile", "NEW", ["context"]),
         ("quality", "NEW", ["profile"]))
    assert engine.get_state("run_1") == {"ready": ["metadata", "profile"]}
    with session_scope() as s:
        assert s.get(AnalysisRun, "run_1").status == "RUNNING"


def test_get_state_caps_ready_at_four(sqlite_db):
    _run(*[(f"t{i}", "NEW", []) for i in range(6)])
    assert engine.get_state("run_1")["ready"] == ["t0", "t1", "t2", "t3"]


def test_get_state_optional_failure_satisfies_dependency(sqlite_db):
    _run(("opt", "FAILED", [], {"optional": True}), ("next", "NEW", ["opt"]))
    assert engine.get_state("run_1") == {"ready": ["next"]}


def test_get_state_required_failure_fails_the_run(sqlite_db):
    _run(("req", "FAILED", []), ("next", "NEW", ["req"]))
    assert engine.get_state("run_1") == {"fail": "required task(s) failed: req"}


def test_get_state_done_and_running(sqlite_db):
    _run(("a", "COMPLETED", []), ("b", "RUNNING", []))
    assert engine.get_state("run_1") == {"running": ["b"]}
    with session_scope() as s:
        s.scalar(select(RunTask).where(RunTask.key == "b")).status = "SKIPPED"
    assert engine.get_state("run_1") == {"done": True}


def test_get_state_deadlock(sqlite_db):
    _run(("a", "NEW", ["missing"]))
    assert engine.get_state("run_1") == {"fail": "no runnable task (dependency deadlock)"}


def test_get_state_waits_for_pending_approval(sqlite_db):
    _run(("publish_request", "COMPLETED", []))
    approval = _approval("pending")
    with session_scope() as s:
        s.add(RunTask(id="tsk_p", run_id="run_1", key="publish", agent_id="publisher", title="publish", status="NEW",
                      depends_on=["publish_request"], input={"approval_id": approval}, output={}, plan_version=1, seq=99))
    assert engine.get_state("run_1") == {"waiting_user": ["publish"]}
    assert _task("publish").status == "WAITING_USER"
    with session_scope() as s:
        assert s.get(AnalysisRun, "run_1").status == "WAITING_USER"
        s.get(Approval, approval).status = "approved"
    assert engine.get_state("run_1") == {"ready": ["publish"]}


def test_get_state_skips_publish_on_rejected_or_missing_approval(sqlite_db):
    _run(("publish_request", "COMPLETED", []), ("publish", "NEW", ["publish_request"]))
    assert engine.get_state("run_1") == {"done": True}  # nothing publishable: publish is skipped
    assert _task("publish").status == "SKIPPED"
    with session_scope() as s:
        t = s.scalar(select(RunTask).where(RunTask.key == "publish"))
        t.status, t.input = "NEW", {"approval_id": _approval("rejected")}
    assert engine.get_state("run_1") == {"done": True}
    assert _task("publish").error == "approval rejected, expired or invalidated"


# ---------------------------------------------------------------------------------- execute_task
@pytest.fixture
def dispatch(monkeypatch):
    """Replace agent dispatch; `calls` records (key, plan version seen by the producing-plan context)."""
    from analystos.agents import dispatch as dispatch_mod
    from analystos.artifacts.registry import producing_plan

    state = SimpleNamespace(calls=[], behaviour=lambda ctx: {"ok": True})

    def fake_dispatch(ctx):
        state.calls.append((ctx.key, producing_plan.get()))
        return state.behaviour(ctx)

    monkeypatch.setattr(dispatch_mod, "dispatch", fake_dispatch)
    monkeypatch.setattr(engine, "RunContext", SimpleNamespace(load=lambda run_id, key, services: SimpleNamespace(key=key)))
    return state


def test_execute_task_completes_and_is_idempotent(sqlite_db, dispatch):
    _run(("a", "NEW", []))
    assert engine.execute_task("run_1", "a", services=object()) == {"status": "COMPLETED", "error": None}
    task = _task("a")
    assert (task.status, task.output, task.attempts) == ("COMPLETED", {"ok": True}, 1)
    assert dispatch.calls == [("a", ("run_1", 1))]  # artifacts written by the task are stamped with plan v1
    assert engine.execute_task("run_1", "a", services=object()) == {"status": "COMPLETED", "cached": True}
    assert len(dispatch.calls) == 1
    with session_scope() as s:
        types = [e.type for e in s.scalars(select(RunEvent).order_by(RunEvent.id))]
    assert types == ["agent.started", "agent.completed"]


def test_execute_task_missing_and_claimed(sqlite_db, dispatch):
    _run(("a", "RUNNING", []))
    assert engine.execute_task("run_1", "nope", services=object()) == {"status": "missing"}
    with session_scope() as s:
        s.scalar(select(RunTask).where(RunTask.key == "a")).started_at = utcnow()
    assert engine.execute_task("run_1", "a", services=object()) == {"status": "in_progress"}
    with session_scope() as s:  # a claim older than the activity timeout belongs to a crashed worker
        s.scalar(select(RunTask).where(RunTask.key == "a")).started_at = utcnow() - timedelta(seconds=engine.CLAIM_TTL_SECONDS + 5)
    assert engine.execute_task("run_1", "a", services=object())["status"] == "COMPLETED"


def test_execute_task_domain_error_fails_without_retry(sqlite_db, dispatch):
    _run(("a", "NEW", []))

    def boom(ctx):
        raise InvalidInput("bad spec")
    dispatch.behaviour = boom
    assert engine.execute_task("run_1", "a", services=object()) == {"status": "FAILED", "error": "invalid_input: bad spec"}
    assert _task("a").status == "FAILED"


def test_execute_task_crash_is_reset_and_reraised_until_last_attempt(sqlite_db, dispatch):
    _run(("a", "NEW", []))

    def crash(ctx):
        raise RuntimeError("worker died")
    dispatch.behaviour = crash
    for _ in range(2):
        with pytest.raises(RuntimeError):
            engine.execute_task("run_1", "a", services=object())
        assert _task("a").status == "NEW"
    assert engine.execute_task("run_1", "a", services=object())["status"] == "FAILED"  # third attempt is final
    assert _task("a").attempts == 3


def test_execute_task_cancelled_without_cancel_control_is_requeued(sqlite_db, dispatch):
    _run(("a", "NEW", []))

    def cancelled(ctx):
        raise RunCancelled("paused")
    dispatch.behaviour = cancelled
    assert engine.execute_task("run_1", "a", services=object()) == {"status": "requeued"}
    assert _task("a").status == "NEW"


def test_execute_task_discards_results_when_replanned_while_running(sqlite_db, dispatch):
    _run(("dataset", "NEW", []), ("test:H1", "NEW", []))

    def replan_midway(ctx):
        with session_scope() as s:
            s.get(AnalysisRun, "run_1").plan_version = 2
        return {"stale": True}
    dispatch.behaviour = replan_midway
    assert engine.execute_task("run_1", "dataset", services=object()) == {"status": "discarded"}
    task = _task("dataset")
    assert (task.status, task.output) == ("NEW", {})  # base task runs again under the new plan
    assert engine.execute_task("run_1", "test:H1", services=object()) == {"status": "discarded"}
    assert _task("test:H1") is None  # dynamic task of the old plan is removed


def test_get_state_locks_only_to_change_state(sqlite_db, monkeypatch):
    """P4-S02: a poll that changes nothing reads without locks; one that must change state decides
    again under the run's row lock."""
    _run(("a", "COMPLETED", []), ("b", "RUNNING", []), ("c", "NEW", ["a"]), status="RUNNING")
    passes, real = [], engine._decide
    monkeypatch.setattr(engine, "_decide", lambda s, run, *, locked: passes.append(locked) or real(s, run, locked=locked))
    assert engine.get_state("run_1") == {"ready": ["c"]}
    assert passes == [False]
    with session_scope() as s:
        s.get(AnalysisRun, "run_1").status = "READY"
    passes.clear()
    assert engine.get_state("run_1") == {"ready": ["c"]}
    assert passes == [False, True]
    with session_scope() as s:
        assert s.get(AnalysisRun, "run_1").status == "RUNNING"
        assert [e.payload for e in s.scalars(select(RunEvent).where(RunEvent.type == "run.status"))] == [{"status": "RUNNING"}]


def test_execute_task_claim_bumps_the_claim_version(sqlite_db, dispatch):
    _run(("a", "NEW", []))
    engine.execute_task("run_1", "a", services=object())
    assert (_task("a").claim_version, _task("a").attempts) == (1, 1)


def test_execute_task_superseded_attempt_is_dropped(sqlite_db, dispatch):
    """An attempt whose claim was retaken (timed out, released, run again) cannot overwrite the result."""
    _run(("a", "NEW", []))

    def retaken(ctx):
        with session_scope() as s:
            t = s.scalar(select(RunTask).where(RunTask.key == "a"))
            t.claim_version, t.status, t.output = t.claim_version + 1, "COMPLETED", {"by": "retry"}
        return {"by": "stale attempt"}
    dispatch.behaviour = retaken
    assert engine.execute_task("run_1", "a", services=object()) == {"status": "superseded"}
    assert (_task("a").status, _task("a").output) == ("COMPLETED", {"by": "retry"})


def test_execute_task_crash_of_a_superseded_attempt_leaves_the_retry_running(sqlite_db, dispatch):
    _run(("a", "NEW", []))

    def crash(ctx):
        with session_scope() as s:  # the retry has claimed the task meanwhile
            t = s.scalar(select(RunTask).where(RunTask.key == "a"))
            t.claim_version, t.started_at = t.claim_version + 1, utcnow()
        raise RuntimeError("worker died")
    dispatch.behaviour = crash
    with pytest.raises(RuntimeError):
        engine.execute_task("run_1", "a", services=object())
    assert _task("a").status == "RUNNING"


def test_execute_task_retakes_a_released_claim(sqlite_db, dispatch):
    from analystos.workflows.activities import release_timed_out_claim

    _run(("a", "RUNNING", []))
    with session_scope() as s:
        s.scalar(select(RunTask).where(RunTask.key == "a")).started_at = utcnow()
    assert engine.execute_task("run_1", "a", services=object()) == {"status": "in_progress"}
    assert release_timed_out_claim("run_1", "a", 2)
    assert engine.execute_task("run_1", "a", services=object())["status"] == "COMPLETED"


# ---------------------------------------------------------------------------------- apply_replan
def test_apply_replan_resets_downstream_and_supersedes_bundle_artifacts(sqlite_db):
    from analystos.agents.visualization import load_bundle_parts
    from analystos.artifacts.registry import producing_plan, save_artifact

    _run(("context", "COMPLETED", []), ("visualize", "COMPLETED", ["context"]), ("test:H1", "COMPLETED", []),
         status="WAITING_USER")
    metric = {"name": "m", "display_name": "M", "definition": "d", "sql_expression": "COUNT(*)"}
    chart = {"key": "finding_i_1", "title": "t", "chart_type": "bar", "intent": "comparison", "dataset": "ds"}
    token = producing_plan.set(("run_1", 1))
    try:
        with session_scope() as s:
            save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="metric", name="m", content=metric)
            save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="chart", name="finding_i_1", content=chart)
    finally:
        producing_plan.reset(token)
    assert [c.key for c in load_bundle_parts("run_1")["charts"]] == ["finding_i_1"]

    with session_scope() as s:
        out = engine.apply_replan(s, s.get(AnalysisRun, "run_1"), "user redirect", full=True)
    assert out["plan_version"] == 2 and out["tasks_removed"] == 1 and out["tasks_reset"] == 1
    assert (_task("visualize").status, _task("visualize").plan_version) == ("NEW", 2)
    assert (_task("context").status, _task("context").plan_version) == ("COMPLETED", 2)  # reused
    with session_scope() as s:
        assert s.get(AnalysisRun, "run_1").status == "RUNNING"
    assert load_bundle_parts("run_1") == {"metrics": [], "charts": [], "dashboards": [], "ids": {}}

    # The re-run under plan v2 re-creates the metric only; a late write from a v1 task cannot overwrite it.
    token = producing_plan.set(("run_1", 2))
    try:
        with session_scope() as s:
            save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="metric", name="m", content=metric)
    finally:
        producing_plan.reset(token)
    token = producing_plan.set(("run_1", 1))
    try:
        with session_scope() as s:
            stale = save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="metric", name="m",
                                  content={**metric, "sql_expression": "COUNT(*) * 100"})
            assert stale.plan_version == 2 and stale.content["sql_expression"] == "COUNT(*)"
    finally:
        producing_plan.reset(token)
    parts = load_bundle_parts("run_1")
    assert [m.name for m in parts["metrics"]] == ["m"] and parts["charts"] == []


def test_save_artifact_outside_a_task_uses_the_runs_current_plan_version(sqlite_db):
    from analystos.artifacts.registry import save_artifact

    _run(plan_version=3)
    with session_scope() as s:
        art = save_artifact(s, workspace_id="ws_1", run_id="run_1", type_="report", name="r", content={"x": 1})
        assert art.plan_version == 3
        assert save_artifact(s, workspace_id="ws_1", type_="report", name="free", content={}).plan_version is None
