"""Durable analysis workflow (ADR-0005). State lives in Postgres; Temporal provides durable
execution, retries with backoff, worker-crash recovery, and event-driven resume via the `nudge`
signal (sent on pause/resume/cancel, approvals and user feedback). The workflow never holds a
database transaction or a request open across model calls.

Each ready step runs on its workload's task queue (`workflows/queues.py`: statistics on `compute`,
BI side effects on `publish`, the rest on `analysis`) with that queue's `start_to_close` and
`heartbeat` timeouts. The run continues-as-new after `max_activities` activities so a long run's
history stays bounded; all state is in Postgres, so the new execution just resumes the loop."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from analystos.workflows.queues import fallback_options, workload_for_step

NON_RETRYABLE = ["Forbidden", "PolicyDenied", "SQLRejected", "ApprovalRequired", "InvalidInput", "BudgetExceeded",
                 "RunCancelled", "NotFound", "Conflict"]
QUICK = dict(start_to_close_timeout=timedelta(minutes=2),
             retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5))
TASK_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                         maximum_interval=timedelta(seconds=30), maximum_attempts=3,
                         non_retryable_error_types=NON_RETRYABLE)


def _queue_kwargs(opts: dict[str, Any], workload: str) -> dict[str, Any]:
    q = opts["queues"][workload]
    return dict(task_queue=q["name"], start_to_close_timeout=timedelta(seconds=q["start_to_close"]),
                heartbeat_timeout=timedelta(seconds=q["heartbeat"]))


@workflow.defn(name="AnalysisWorkflow")
class AnalysisWorkflow:
    def __init__(self) -> None:
        self._nudged = False

    @workflow.signal
    def nudge(self) -> None:
        self._nudged = True

    async def _wait(self, seconds: int) -> None:
        self._nudged = False
        with contextlib.suppress(asyncio.TimeoutError):
            await workflow.wait_condition(lambda: self._nudged, timeout=timedelta(seconds=seconds))

    @workflow.run
    async def run(self, run_id: str, opts: dict[str, Any] | None = None) -> str:
        opts = opts or fallback_options(workflow.info().task_queue.removesuffix("-analysis"))
        analysis = _queue_kwargs(opts, "analysis")
        quick = {**QUICK, "task_queue": analysis["task_queue"]}
        plan = {**analysis, "start_to_close_timeout": timedelta(minutes=10), "retry_policy": RetryPolicy(maximum_attempts=3)}
        activities = 0
        if not opts.get("resumed"):
            await workflow.execute_activity("plan_run", run_id, **plan)
            activities += 1
        while True:
            if activities >= opts.get("max_activities", 400) or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(args=[run_id, {**opts, "resumed": True}])
            state = await workflow.execute_activity("get_state", run_id, **quick)
            activities += 1
            if state.get("terminal"):
                return state.get("status", "COMPLETED")
            if state.get("control") == "cancel":
                await workflow.execute_activity("finish_run", args=[run_id, "CANCELLED", None], **quick)
                return "CANCELLED"
            if state.get("needs_plan"):
                await workflow.execute_activity("plan_run", run_id, **plan)
                activities += 1
                continue
            if state.get("fail"):
                await workflow.execute_activity("finish_run", args=[run_id, "FAILED", state["fail"]], **quick)
                return "FAILED"
            if state.get("done"):
                await workflow.execute_activity("finish_run", args=[run_id, "COMPLETED", None], **quick)
                return "COMPLETED"
            if state.get("ready"):
                results = await asyncio.gather(*[
                    workflow.execute_activity("execute_task", args=[run_id, key], retry_policy=TASK_RETRY,
                                              **_queue_kwargs(opts, workload_for_step(key)))
                    for key in state["ready"]], return_exceptions=True)
                activities += len(state["ready"])
                for r in results:
                    if isinstance(r, BaseException):
                        workflow.logger.warning("task activity failed after retries: %s", r)
                continue
            # paused, waiting for a person, or tasks still running elsewhere: wait for a signal (or re-check)
            await self._wait(300 if state.get("control") == "pause" or state.get("waiting_user") else 15)


@workflow.defn(name="CrawlWorkflow")
class CrawlWorkflow:
    """One metadata crawl on the `crawl` pool. `run_crawl` records failure on the crawl_run row
    itself, so it is not retried (a retry would re-run a crawl the user already saw fail)."""

    @workflow.run
    async def run(self, crawl_id: str, user_id: str, opts: dict[str, Any] | None = None) -> dict:
        opts = opts or fallback_options(workflow.info().task_queue.removesuffix("-crawl"))
        return await workflow.execute_activity("run_crawl", args=[crawl_id, user_id], retry_policy=RetryPolicy(maximum_attempts=1),
                                               **_queue_kwargs(opts, "crawl"))
