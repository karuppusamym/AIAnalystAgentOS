"""Start/signal runs on the configured orchestrator. `temporal` in deployments; `local` runs the
same engine loop in a background thread (tests, single-process demos)."""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future

from analystos.core.config import get_settings
from analystos.core.logging import get_logger
from analystos.runtime import engine

log = get_logger(__name__)
_loop: asyncio.AbstractEventLoop | None = None
_client = None
_lock = threading.Lock()
_local_active: set[str] = set()  # runs this process's local orchestrator is driving


def _event_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="temporal-client").start()
    return _loop


def _run(coro) -> object:
    fut: Future = asyncio.run_coroutine_threadsafe(coro, _event_loop())
    return fut.result(timeout=30)


async def _temporal_client():
    global _client
    if _client is None:
        from temporalio.client import Client

        s = get_settings()
        _client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    return _client


def workflow_id(run_id: str) -> str:
    return f"analysis-{run_id}"


def start_run(run_id: str) -> str:
    settings = get_settings()
    if settings.orchestrator == "temporal":
        async def go():
            from temporalio.exceptions import WorkflowAlreadyStartedError

            from analystos.workflows.queues import queue_name, workflow_options

            client = await _temporal_client()
            try:
                await client.start_workflow("AnalysisWorkflow", args=[run_id, workflow_options()], id=workflow_id(run_id),
                                            task_queue=queue_name(settings.temporal_queue_prefix, "analysis"))
            except WorkflowAlreadyStartedError:  # idempotent: the loop is alive, just wake it
                await client.get_workflow_handle(workflow_id(run_id)).signal("nudge")
        _run(go())
        return workflow_id(run_id)
    with _lock:
        if run_id in _local_active:  # a re-delivered dispatch (outbox relay): this process already drives it
            return f"local-{run_id}"
        _local_active.add(run_id)

    def drive() -> None:
        try:
            run_local(run_id)
        finally:
            with _lock:
                _local_active.discard(run_id)
    threading.Thread(target=drive, daemon=True, name=f"run-{run_id}").start()
    return f"local-{run_id}"


def signal_run(run_id: str) -> None:
    if get_settings().orchestrator != "temporal":
        return  # the local loop polls

    async def go():
        client = await _temporal_client()
        await client.get_workflow_handle(workflow_id(run_id)).signal("nudge")
    try:
        _run(go())
    except Exception as exc:  # the workflow also re-checks state periodically
        log.warning("signal to %s failed: %s", run_id, exc)


def start_crawl_job(crawl_id: str, user_id: str) -> str | None:
    """Run a started crawl on the Temporal `crawl` pool. None when the orchestrator is local or Temporal
    cannot take it; the caller then runs the crawl in-process as before."""
    settings = get_settings()
    if settings.orchestrator != "temporal":
        return None

    async def go():
        from analystos.workflows.queues import queue_name, workflow_options

        client = await _temporal_client()
        await client.start_workflow("CrawlWorkflow", args=[crawl_id, user_id, workflow_options()], id=f"crawl-{crawl_id}",
                                    task_queue=queue_name(settings.temporal_queue_prefix, "crawl"))
    try:
        _run(go())
    except Exception as exc:
        log.warning("crawl %s not handed to Temporal (%s); running it in-process", crawl_id, exc)
        return None
    return f"crawl-{crawl_id}"


def run_recipe_compute(job: dict) -> dict:
    """A recipe's snapshot statement on the Temporal `compute` pool (ADR-0023); in-process when the
    orchestrator is local or Temporal cannot take it (the job only reads snapshot files, so both paths
    give the same result)."""
    from analystos.recipes.execute import run_snapshot_job

    settings = get_settings()
    if settings.orchestrator != "temporal":
        return run_snapshot_job(job)

    async def go():
        from analystos.core.ids import new_id
        from analystos.workflows.queues import queue_name, workflow_options

        client = await _temporal_client()
        return await client.execute_workflow("RecipeComputeWorkflow", args=[job, workflow_options()],
                                             id=new_id("recipe-compute"),
                                             task_queue=queue_name(settings.temporal_queue_prefix, "analysis"))
    try:
        fut: Future = asyncio.run_coroutine_threadsafe(go(), _event_loop())
        return fut.result(timeout=900)
    except Exception as exc:
        log.warning("recipe compute not handed to Temporal (%s); running it in-process", exc)
        return run_snapshot_job(job)


def run_local(run_id: str, *, poll: float = 0.3, max_seconds: float = 3600) -> str:
    """Same control loop as the Temporal workflow, synchronous."""
    started = time.time()
    engine.plan_run(run_id)
    while time.time() - started < max_seconds:
        state = engine.get_state(run_id)
        if state.get("terminal"):
            return state.get("status", "COMPLETED")
        if state.get("control") == "cancel":
            engine.finish_run(run_id, "CANCELLED")
            return "CANCELLED"
        if state.get("needs_plan"):
            engine.plan_run(run_id)
            continue
        if state.get("fail"):
            engine.finish_run(run_id, "FAILED", state["fail"])
            return "FAILED"
        if state.get("done"):
            engine.finish_run(run_id, "COMPLETED")
            return "COMPLETED"
        if state.get("ready"):
            for key in state["ready"]:
                for _attempt in range(3):
                    try:
                        engine.execute_task(run_id, key)
                        break
                    except Exception as exc:  # retried like a Temporal activity
                        log.warning("local task %s attempt failed: %s", key, exc)
                        time.sleep(0.5)
            continue
        time.sleep(poll)
    engine.finish_run(run_id, "FAILED", "local orchestrator timeout")
    return "FAILED"
