from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from analystos.core.config import get_settings
from analystos.core.logging import configure_logging, get_logger

log = get_logger(__name__)


async def _main() -> None:
    from temporalio.client import Client
    from temporalio.worker import Worker

    from analystos.workflows.activities import ACTIVITIES
    from analystos.workflows.analysis_workflow import AnalysisWorkflow

    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        worker = Worker(client, task_queue=settings.temporal_task_queue, workflows=[AnalysisWorkflow],
                        activities=ACTIVITIES, activity_executor=pool, max_concurrent_activities=8)
        log.info("temporal worker listening on %s", settings.temporal_task_queue)
        await worker.run()


def run_worker() -> None:
    configure_logging()
    asyncio.run(_main())
