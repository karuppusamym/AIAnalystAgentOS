"""Durable analysis workflow (ADR-0005). State lives in Postgres; Temporal provides durable
execution, retries with backoff, worker-crash recovery, and event-driven resume via the `nudge`
signal (sent on pause/resume/cancel, approvals and user feedback). The workflow never holds a
database transaction or a request open across model calls."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

NON_RETRYABLE = ["Forbidden", "PolicyDenied", "SQLRejected", "ApprovalRequired", "InvalidInput", "BudgetExceeded",
                 "RunCancelled", "NotFound", "Conflict"]
QUICK = dict(start_to_close_timeout=timedelta(minutes=2),
             retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5))
TASK = dict(start_to_close_timeout=timedelta(minutes=30), heartbeat_timeout=None,
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                                     maximum_interval=timedelta(seconds=30), maximum_attempts=3,
                                     non_retryable_error_types=NON_RETRYABLE))


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
    async def run(self, run_id: str) -> str:
        await workflow.execute_activity("plan_run", run_id, start_to_close_timeout=timedelta(minutes=10),
                                        retry_policy=RetryPolicy(maximum_attempts=3))
        while True:
            state = await workflow.execute_activity("get_state", run_id, **QUICK)
            if state.get("terminal"):
                return state.get("status", "COMPLETED")
            if state.get("control") == "cancel":
                await workflow.execute_activity("finish_run", args=[run_id, "CANCELLED", None], **QUICK)
                return "CANCELLED"
            if state.get("needs_plan"):
                await workflow.execute_activity("plan_run", run_id, start_to_close_timeout=timedelta(minutes=10))
                continue
            if state.get("fail"):
                await workflow.execute_activity("finish_run", args=[run_id, "FAILED", state["fail"]], **QUICK)
                return "FAILED"
            if state.get("done"):
                await workflow.execute_activity("finish_run", args=[run_id, "COMPLETED", None], **QUICK)
                return "COMPLETED"
            if state.get("ready"):
                results = await asyncio.gather(*[workflow.execute_activity("execute_task", args=[run_id, key], **TASK)
                                                 for key in state["ready"]], return_exceptions=True)
                for r in results:
                    if isinstance(r, BaseException):
                        workflow.logger.warning("task activity failed after retries: %s", r)
                continue
            # paused, waiting for a person, or tasks still running elsewhere: wait for a signal (or re-check)
            await self._wait(300 if state.get("control") == "pause" or state.get("waiting_user") else 15)
