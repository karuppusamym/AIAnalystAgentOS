"""The Temporal transport of an isolated pool (ADR-0022 decisions 1 and 5, P7-06).

The isolated worker registers one activity, `run_isolated_task`, on `<prefix>-<pool>`. It heartbeats
while the job runs (so a lost worker is a heartbeat timeout and a retry under the same idempotency
key) and signals each task event to the `IsolatedTaskWorkflow` that started it; the workflow, on the
analysis queue, persists them through the control plane (`record_task_events`). A failure the job
marks retryable is raised so Temporal retries it; any other failure is returned as data.
"""
from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from analystos.contracts.worker import TaskDispatch, TaskEvent

_config: Any = None
HEARTBEAT_SECONDS = 5.0


@activity.defn(name="run_isolated_task")
async def run_isolated_task(dispatch: dict) -> dict:
    from analystos.workers.runtime import execute

    config = _config
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
    info = activity.info()
    handle = activity.client().get_workflow_handle(info.workflow_id) if info.workflow_id else None
    work = loop.run_in_executor(None, lambda: execute(TaskDispatch.model_validate(dispatch), config,
                                                      emit=lambda e: loop.call_soon_threadsafe(queue.put_nowait, e)))
    interval = HEARTBEAT_SECONDS
    if info.heartbeat_timeout:
        interval = max(min(interval, info.heartbeat_timeout.total_seconds() / 3), 0.2)
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=interval)
        except TimeoutError:
            event = None
        if event is not None:
            activity.heartbeat({"event": event.type, "seq": event.seq})
            if handle is not None:
                with contextlib.suppress(Exception):  # the workflow also records a synthetic failure on timeout
                    await handle.signal("task_event", event.model_dump(mode="json"))
        else:
            activity.heartbeat({"alive": True})
        if work.done() and queue.empty():
            break
    result = work.result()
    body = result.model_dump(mode="json")
    if result.error is not None and result.error.retryable:
        raise ApplicationError(result.error.message, body, type=result.error.code, non_retryable=False)
    return body


def build_isolated_workers(client: Any, config: Any, *, prefix: str, max_concurrent: dict[str, int] | None = None) -> list[Any]:
    from temporalio.worker import Worker

    from analystos.workflows.queues import load_config, queue_name

    global _config
    _config = config
    specs = load_config()[0]
    workers = []
    for pool in config.pools:
        slots = (max_concurrent or {}).get(pool) or specs[pool].max_concurrent
        workers.append(Worker(client, task_queue=queue_name(prefix, pool), activities=[run_isolated_task],
                              activity_executor=ThreadPoolExecutor(max_workers=slots + 2, thread_name_prefix=f"iso-{pool}"),
                              max_concurrent_activities=slots))
    return workers


def run_temporal_worker(config: Any, *, address: str, namespace: str, prefix: str) -> None:
    from temporalio.client import Client

    async def go() -> None:
        client = await Client.connect(address, namespace=namespace)
        workers = build_isolated_workers(client, config, prefix=prefix)
        await asyncio.gather(*(w.run() for w in workers))
    asyncio.run(go())
