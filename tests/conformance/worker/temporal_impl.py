"""The Temporal implementation of an isolated pool, for the conformance suite (`ANALYSTOS_CONFORMANCE_IMPL=temporal`).

A Temporal dev server (`temporalio.testing.WorkflowEnvironment.start_local`), the real `IsolatedTaskWorkflow`
hosted by an in-process control-plane worker on `<prefix>-analysis` (its `record_task_events` activity is a
recorder standing in for the database), and one real isolated worker *process* per pool,
`python -m analystos.workers.main --queues <pool> --conformance`, started with only the variables an isolated
deployment gets. Dispatches go through `TemporalTransport.run_async`, exactly as the control plane sends them.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from temporalio import activity

from analystos.contracts.worker import ISOLATED_POOLS, TaskDispatch, TaskEvent, TaskResult

_listeners: dict[str, Any] = {}


@activity.defn(name="record_task_events")
def record_task_events(task_id: str, events: list[dict]) -> int:
    listener = _listeners.get(task_id)
    for e in events:
        if listener is not None:
            listener(TaskEvent.model_validate(e))
    return len(events)


class TemporalPool:
    records_events = False  # events reach on_event through the workflow's record activity, as persisted

    def __init__(self, artifact_url: str) -> None:
        self.artifact_url = artifact_url
        self.prefix = f"aosconf-{uuid.uuid4().hex[:8]}"
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="conformance-temporal")
        self.thread.start()
        self.procs: list[subprocess.Popen] = []
        self.logs: list[Any] = []
        self._call(self._start())

    def _call(self, coro: Any, timeout: float = 300) -> Any:
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    async def _start(self) -> None:
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker

        from analystos.workers.dispatch import TemporalTransport
        from analystos.workflows.analysis_workflow import IsolatedTaskWorkflow
        from analystos.workflows.queues import load_config, queue_name, queue_options

        self.env = await WorkflowEnvironment.start_local()
        client = self.env.client
        self.worker = Worker(client, task_queue=queue_name(self.prefix, "analysis"), workflows=[IsolatedTaskWorkflow],
                             activities=[record_task_events],
                             activity_executor=__import__("concurrent.futures").futures.ThreadPoolExecutor(4))
        self.worker_task = asyncio.ensure_future(self.worker.run())
        specs, max_activities = load_config()
        self.transport = TemporalTransport(client=client, prefix=self.prefix,
                                           options=queue_options(self.prefix, specs, max_activities))
        address = client.service_client.config.target_host
        import analystos

        src = str(Path(analystos.__file__).resolve().parents[1])
        for pool in ISOLATED_POOLS:
            log = tempfile.TemporaryFile()  # noqa: SIM115 - closed with the pool
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": src, "ANALYSTOS_WORKER_QUEUES": pool,
                   "ANALYSTOS_WORKER_ARTIFACT_URL": self.artifact_url, "ANALYSTOS_TEMPORAL_ADDRESS": address,
                   "ANALYSTOS_TEMPORAL_QUEUE_PREFIX": self.prefix, "HOME": tempfile.gettempdir()}
            self.procs.append(subprocess.Popen([sys.executable, "-m", "analystos.workers.main", "--conformance"],  # noqa: S603
                                               env=env, stdout=log, stderr=log, cwd=tempfile.gettempdir()))
            self.logs.append(log)

    def run(self, dispatch: TaskDispatch, on_event: Any) -> TaskResult:
        _listeners[dispatch.envelope.task_id] = on_event
        try:
            return self._call(self.transport.run_async(dispatch), timeout=dispatch.envelope.budget.wall_seconds * 4 + 120)
        finally:
            _listeners.pop(dispatch.envelope.task_id, None)

    def worker_log(self) -> str:
        out = []
        for f in self.logs:
            f.seek(0)
            out.append(f.read().decode("utf-8", "replace")[-3000:])
        return "\n---\n".join(out)

    def close(self) -> None:
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

        async def stop() -> None:
            self.worker_task.cancel()
            await self.env.shutdown()
        with contextlib.suppress(Exception):  # best-effort teardown
            self._call(stop(), timeout=60)
        self.loop.call_soon_threadsafe(self.loop.stop)


def factory(artifact_url: str) -> TemporalPool:
    return TemporalPool(artifact_url)
