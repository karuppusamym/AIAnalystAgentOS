"""Temporal workers, one pool per workload queue (spec v3 §8, P4-S01).

`analystos worker --queues analysis,compute` serves only those queues, so each pool is deployed and
scaled on its own (compose `worker-*` services, `deploy/helm/values.workers.yaml`). The compute pool
runs activities in a bounded *process* pool: CPU-bound statistics neither block this process's event
loop nor stop its heartbeats, and a statistic hung in native code only freezes its own pool process.
"""
from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any

from analystos.core.config import get_settings
from analystos.core.logging import configure_logging, get_logger
from analystos.workflows.queues import QueueSpec, load_config, parse_workloads, queue_name

log = get_logger(__name__)


def build_workers(client: Any, workloads: list[str], *, prefix: str, stack: contextlib.ExitStack,
                  specs: dict[str, QueueSpec] | None = None, activities: dict[str, list] | None = None,
                  workflows: dict[str, list] | None = None) -> list[Any]:
    """One Worker per workload. `activities`/`workflows` override the defaults (tests register probes)."""
    from temporalio.worker import SharedStateManager, Worker

    from analystos.workflows.activities import BY_WORKLOAD
    from analystos.workflows.analysis_workflow import AnalysisWorkflow, CrawlWorkflow

    specs = specs or load_config()[0]
    activities = activities or BY_WORKLOAD
    workflows = workflows or {"analysis": [AnalysisWorkflow], "crawl": [CrawlWorkflow]}
    workers = []
    for workload in workloads:
        spec = specs[workload]
        kwargs: dict[str, Any] = {}
        if spec.executor == "process":
            # spawn, not fork: a forked child would inherit this process's DB pools and Temporal threads.
            # The manager outlives the pool (entered first, exited last): pool processes heartbeat through it.
            ctx = multiprocessing.get_context("spawn")
            manager = stack.enter_context(ctx.Manager())
            executor = stack.enter_context(ProcessPoolExecutor(max_workers=spec.max_concurrent, mp_context=ctx,
                                                               max_tasks_per_child=spec.max_tasks_per_child))
            kwargs["shared_state_manager"] = SharedStateManager.create_from_multiprocessing(manager)
        else:
            executor = stack.enter_context(ThreadPoolExecutor(max_workers=spec.max_concurrent,
                                                              thread_name_prefix=f"act-{workload}"))
        workers.append(Worker(client, task_queue=queue_name(prefix, workload), workflows=workflows.get(workload, []),
                              activities=activities.get(workload, []), activity_executor=executor,
                              max_concurrent_activities=spec.max_concurrent, **kwargs))
        log.info("temporal worker: queue %s (%s pool of %d)", queue_name(prefix, workload), spec.executor, spec.max_concurrent)
    return workers


async def _main(workloads: list[str]) -> None:
    from temporalio.client import Client

    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    with contextlib.ExitStack() as stack:
        workers = build_workers(client, workloads, prefix=settings.temporal_queue_prefix, stack=stack)
        await asyncio.gather(*(w.run() for w in workers))


def run_worker(queues: str | None = None) -> None:
    configure_logging()
    asyncio.run(_main(parse_workloads(queues if queues is not None else get_settings().worker_queues)))
