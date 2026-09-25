"""Orchestrator-agnostic run engine. Temporal (production) and the local runner (tests, laptops)
drive the same four operations: plan_run, get_state, execute_task, finish_run. All state is in
Postgres; each task has a stable key (idempotency) and a plan_version (stale results are dropped)."""
from __future__ import annotations

import traceback
from typing import Any

from sqlalchemy import delete, select, update

from analystos.contracts.events import RUN_TERMINAL
from analystos.core.errors import AnalystOSError, RunCancelled
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger, run_id_var
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Hypothesis, Insight, RunTask
from analystos.events.bus import emit
from analystos.governance.approvals import invalidate_run_approvals
from analystos.runtime.context import RunContext, Services, default_services
from analystos.runtime.plan import DYNAMIC_PREFIXES, REPLAN_RESET, dep_satisfied, plan_hash

log = get_logger(__name__)
CLAIM_TTL_SECONDS = 30 * 60  # equals the Temporal start_to_close timeout for execute_task
NON_RETRYABLE = ("Forbidden", "PolicyDenied", "SQLRejected", "ApprovalRequired", "InvalidInput", "BudgetExceeded",
                 "RunCancelled", "NotFound", "Conflict")


def set_run_status(session, run: AnalysisRun, status: str, **extra: Any) -> None:
    if run.status == status and not extra:
        return
    run.status = status
    for k, v in extra.items():
        setattr(run, k, v)
    if status in RUN_TERMINAL and run.finished_at is None:
        run.finished_at = utcnow()
    emit(run.workspace_id, "run.status", {"status": status, **{k: v for k, v in extra.items() if k == "error"}},
         run_id=run.id, session=session)


def add_task(session, run: AnalysisRun, *, key: str, agent: str, title: str, depends_on: list[str],
             optional: bool = False, input: dict | None = None, seq: int = 0, from_version: int | None = None) -> RunTask:
    """Add a task to the run's current plan. `from_version` is the plan version of the task doing the
    adding; a task running under a superseded plan may not extend the new one."""
    if from_version is not None and from_version != run.plan_version:
        raise RunCancelled("plan changed while this task was running; not extending the new plan")
    existing = session.scalar(select(RunTask).where(RunTask.run_id == run.id, RunTask.key == key))
    if existing:
        return existing
    task = RunTask(id=new_id("tsk"), run_id=run.id, key=key, agent_id=agent, title=title, depends_on=depends_on,
                   input={**(input or {}), "optional": optional}, plan_version=run.plan_version, seq=seq, status="NEW")
    session.add(task)
    return task


def materialize_plan(session, run: AnalysisRun) -> None:
    skip_publish = (run.origin or {}).get("publish") == "skip"
    for i, step in enumerate(run.plan["steps"]):
        task = add_task(session, run, key=step["key"], agent=step["agent"], title=step["title"], depends_on=step["depends_on"],
                        optional=step.get("optional", False), seq=i * 10)
        if skip_publish and step["key"] in ("publish_request", "publish"):
            # Scheduled re-analyses and alert investigations refresh findings and reports; they do not
            # propose publication unless the schedule asks for it.
            task.status, task.error = "SKIPPED", "publication not requested for this run"
    run.plan_hash = plan_hash(run.plan, constraints=run.constraints, scope_hash=run.scope.get("hash", ""),
                              plan_version=run.plan_version)


def plan_run(run_id: str, services: Services | None = None) -> dict:
    """Activity 1: the supervisor builds the plan (idempotent)."""
    from analystos.agents.supervisor import build_plan

    services = services or default_services()
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        if run.plan_version > 0:
            return {"plan_version": run.plan_version}
        set_run_status(s, run, "PLANNING", started_at=utcnow())
    plan = build_plan(run_id, services)
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id, with_for_update=True)
        if run.plan_version > 0:
            return {"plan_version": run.plan_version}
        run.plan = plan
        run.plan_version = 1
        materialize_plan(s, run)
        set_run_status(s, run, "READY")
        emit(run.workspace_id, "run.replanned", {"plan_version": 1, "plan_hash": run.plan_hash, "steps": len(plan["steps"])},
             run_id=run.id, session=s)
        if any(st["key"] == "plan_approval" for st in plan["steps"]):
            from analystos.governance.approvals import request_approval

            approval = request_approval(s, workspace_id=run.workspace_id, run_id=run.id, action="execute_plan",
                                        payload={"plan": plan, "constraints": run.constraints}, plan_hash=run.plan_hash,
                                        policy_version=run.policy_version, requested_by=run.requested_by, risk_tier="medium",
                                        destination=None, affected_assets=run.scope.get("assets", []))
            task = s.scalar(select(RunTask).where(RunTask.run_id == run.id, RunTask.key == "plan_approval"))
            task.input = {**task.input, "approval_id": approval.id}
    return {"plan_version": 1}


def _approval_gate(session, task: RunTask) -> str | None:
    """For approval-gated tasks: 'ready' | 'waiting' | 'skip'."""
    approval_id = task.input.get("approval_id")
    if task.key not in ("plan_approval", "publish") or not approval_id:
        return None
    approval = session.get(Approval, approval_id)
    if approval is None:
        return "skip"
    if approval.status in ("approved", "executed"):
        return "ready"
    if approval.status == "pending":
        return "waiting"
    return "skip"


def get_state(run_id: str) -> dict:
    """Activity: what should the orchestrator do next?"""
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id, with_for_update=True)
        if run.status in RUN_TERMINAL:
            return {"terminal": True, "status": run.status}
        if run.control == "cancel":
            return {"control": "cancel"}
        if run.control == "pause":
            if run.status != "PAUSED":
                set_run_status(s, run, "PAUSED")
            return {"control": "pause"}
        tasks = {t.key: t for t in s.scalars(select(RunTask).where(RunTask.run_id == run_id))}
        if not tasks:
            return {"needs_plan": True}
        ready, waiting, running = [], [], []
        for key, task in sorted(tasks.items(), key=lambda kv: (kv[1].seq, kv[0])):
            if task.status == "RUNNING":
                running.append(key)
                continue
            if task.status not in ("NEW", "WAITING_USER"):
                continue
            if not all(dep_satisfied(d, tasks, key) for d in task.depends_on):
                continue
            gate = _approval_gate(s, task)
            if task.key == "publish" and not task.input.get("approval_id") and task.status == "NEW":
                # publish_request finished without creating an approval (nothing publishable or denied)
                task.status, task.finished_at = "SKIPPED", utcnow()
                continue
            if gate == "waiting":
                if task.status != "WAITING_USER":
                    task.status = "WAITING_USER"
                waiting.append(key)
            elif gate == "skip":
                task.status, task.finished_at, task.error = "SKIPPED", utcnow(), "approval rejected, expired or invalidated"
                emit(run.workspace_id, "task.updated", {"task": key, "status": "SKIPPED"}, run_id=run_id, session=s)
            else:
                ready.append(key)
        failed_required = [k for k, t in tasks.items() if t.status == "FAILED" and not t.input.get("optional")]
        if failed_required:
            return {"fail": f"required task(s) failed: {', '.join(failed_required)}"}
        done = all(t.status in ("COMPLETED", "SKIPPED", "FAILED", "CANCELLED", "INVALIDATED") for t in tasks.values())
        if done:
            return {"done": True}
        if ready:
            if run.status not in ("RUNNING",):
                set_run_status(s, run, "RUNNING")
            return {"ready": ready[:4]}
        if waiting and not running:
            if run.status != "WAITING_USER":
                set_run_status(s, run, "WAITING_USER")
            return {"waiting_user": waiting}
        if running:
            return {"running": running}
        return {"fail": "no runnable task (dependency deadlock)"}


def execute_task(run_id: str, key: str, services: Services | None = None) -> dict:
    """Activity: run one task. Idempotent by (run_id, key, plan_version)."""
    from analystos.agents.dispatch import dispatch

    services = services or default_services()
    token = run_id_var.set(run_id)
    try:
        with session_scope() as s:
            task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key).with_for_update())
            if task is None:
                return {"status": "missing"}
            if task.status in ("COMPLETED", "SKIPPED"):
                return {"status": task.status, "cached": True}
            if task.status == "RUNNING" and task.started_at and (utcnow() - task.started_at).total_seconds() < CLAIM_TTL_SECONDS:
                # Another worker holds it. A claim older than the activity timeout is from a crashed worker and is retaken.
                return {"status": "in_progress"}
            task.status, task.attempts, task.started_at, task.error = "RUNNING", task.attempts + 1, utcnow(), None
            version = task.plan_version
            run = s.get(AnalysisRun, run_id)
            emit(run.workspace_id, "agent.started", {"task": key, "agent": task.agent_id, "attempt": task.attempts},
                 run_id=run_id, session=s)
        try:
            ctx = RunContext.load(run_id, key, services)
            output = dispatch(ctx) or {}
            status, error = "COMPLETED", None
        except RunCancelled as exc:
            output, status, error = {}, "CANCELLED", str(exc)
        except AnalystOSError as exc:
            output, status, error = {}, "FAILED", f"{exc.code}: {exc.message}"
            log.warning("task %s failed: %s", key, error)
        except Exception as exc:  # unexpected: keep the trace for the agent console
            output, status, error = {}, "FAILED", f"{type(exc).__name__}: {exc}"
            log.error("task %s crashed\n%s", key, traceback.format_exc())
            if not _is_last_attempt(run_id, key):
                _reset(run_id, key, error)
                raise
        with session_scope() as s:
            task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key).with_for_update())
            run = s.get(AnalysisRun, run_id)
            if task is None:
                return {"status": "discarded"}
            if run.plan_version != version or task.plan_version != version:
                # replanned while running: discard. Dynamic tasks of the old plan are removed; base
                # tasks run again under the new plan version.
                if key.startswith(DYNAMIC_PREFIXES):
                    s.delete(task)
                else:
                    task.status, task.output, task.error = "NEW", {}, "discarded: plan changed while running"
                return {"status": "discarded"}
            if status == "CANCELLED" and run.control != "cancel":
                task.status, task.error = "NEW", None
                return {"status": "requeued"}
            task.status, task.output, task.error, task.finished_at = status, output, error, utcnow()
            emit(run.workspace_id, "agent.completed" if status == "COMPLETED" else "agent.failed",
                 {"task": key, "agent": task.agent_id, "status": status, "error": error}, run_id=run_id, session=s)
        return {"status": status, "error": error}
    finally:
        run_id_var.reset(token)


def _is_last_attempt(run_id: str, key: str, max_attempts: int = 3) -> bool:
    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key))
        return task is None or task.attempts >= max_attempts


def _reset(run_id: str, key: str, error: str) -> None:
    with session_scope() as s:
        s.execute(update(RunTask).where(RunTask.run_id == run_id, RunTask.key == key).values(status="NEW", error=error))


def finish_run(run_id: str, outcome: str, error: str | None = None) -> None:
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id, with_for_update=True)
        if run.status in RUN_TERMINAL:
            return
        notify_needed = False
        if outcome == "CANCELLED":
            s.execute(update(RunTask).where(RunTask.run_id == run_id, RunTask.status.in_(["NEW", "READY", "WAITING_USER"]))
                      .values(status="CANCELLED"))
            invalidate_run_approvals(s, run_id, "run cancelled")
            published = s.scalar(select(RunTask.status).where(RunTask.run_id == run_id, RunTask.key == "publish")) == "COMPLETED"
            run.summary = {**(run.summary or {}), "cancel_outcome": "cancelled_after_publication" if published else "cancelled_before_side_effects"}
        set_run_status(s, run, outcome, **({"error": error} if error else {}))
        notify_needed = (run.origin or {}).get("type") in ("schedule", "alert")
    if notify_needed:
        from analystos.services.schedules import complete_from_run

        complete_from_run(run_id)


DOWNSTREAM_OF_VERIFY = {"dataset", "semantic", "visualize", "publish_request", "publish", "finalize"}


def apply_replan(session, run: AnalysisRun, reason: str, *, full: bool = True) -> dict:
    """Dynamic replanning (§40): persist, mark impacted, cancel invalid pending work, reuse valid artifacts.

    full=True  (redirect / deeper analysis): hypotheses onward are recomputed; context, metadata,
               profile and quality results are reused.
    full=False (finding rejected / metric edited): only dataset onward is recomputed.
    """
    reset_keys = REPLAN_RESET if full else DOWNSTREAM_OF_VERIFY
    run.plan_version += 1
    tasks = list(session.scalars(select(RunTask).where(RunTask.run_id == run.id)))
    removed, reset = 0, 0
    for task in tasks:
        if full and task.key.startswith(DYNAMIC_PREFIXES):
            session.delete(task)
            removed += 1
        elif task.key in reset_keys:
            task.status, task.output, task.error, task.plan_version = "NEW", {}, None, run.plan_version
            task.started_at = task.finished_at = None
            reset += 1
        else:
            task.plan_version = run.plan_version  # still valid (context, metadata, profile, quality): reused
    if full:
        session.execute(update(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.status != "superseded")
                        .values(status="superseded"))
        session.execute(update(Insight).where(Insight.run_id == run.id).values(status="superseded"))
    invalidated = invalidate_run_approvals(session, run.id, f"replanned: {reason}")
    run.plan_hash = plan_hash(run.plan, constraints=run.constraints, scope_hash=run.scope.get("hash", ""),
                              plan_version=run.plan_version)
    if run.status in ("WAITING_USER", "COMPLETED", "PAUSED"):
        run.status = "RUNNING"
        run.finished_at = None
    emit(run.workspace_id, "run.replanned", {"plan_version": run.plan_version, "plan_hash": run.plan_hash,
                                             "reason": reason, "tasks_reset": reset, "tasks_removed": removed,
                                             "approvals_invalidated": invalidated}, run_id=run.id, session=session)
    return {"plan_version": run.plan_version, "tasks_reset": reset, "tasks_removed": removed, "approvals_invalidated": invalidated}


def clear_dynamic(session, run_id: str) -> None:
    session.execute(delete(RunTask).where(RunTask.run_id == run_id, RunTask.key.like("test:%")))
