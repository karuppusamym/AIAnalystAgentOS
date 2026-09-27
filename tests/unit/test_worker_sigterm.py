"""`analystos worker` shuts its Temporal workers down on SIGTERM, so the compute process pool is closed rather than
orphaned (live 2026-09-27: a `kill` of the worker left six pool processes holding database connections)."""
from __future__ import annotations

import asyncio
import os
import signal

import pytest

from analystos.workflows import worker as worker_mod


class FakeWorker:
    def __init__(self):
        self.stopped = asyncio.Event()
        self.shutdown_called = False

    async def run(self):
        await self.stopped.wait()

    async def shutdown(self):
        self.shutdown_called = True
        self.stopped.set()


@pytest.mark.skipif(not hasattr(signal, "SIGTERM") or os.name != "posix", reason="POSIX signal delivery")
def test_sigterm_shuts_the_workers_down_and_returns():
    workers = [FakeWorker(), FakeWorker()]

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(worker_mod._serve_until_terminated(workers), timeout=5)

    asyncio.run(scenario())
    assert all(w.shutdown_called for w in workers)


def test_workers_that_stop_on_their_own_are_not_shut_down_again():
    class Done(FakeWorker):
        async def run(self):
            return None

    workers = [Done()]
    asyncio.run(worker_mod._serve_until_terminated(workers))
    assert workers[0].shutdown_called is False
