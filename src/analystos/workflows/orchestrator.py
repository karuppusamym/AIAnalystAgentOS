"""Start/signal runs on the configured orchestrator. `temporal` in standard/scale deployments; `local`
runs the same engine loop in the API process (the lite profile, tests, laptops; ADR-0025)."""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from analystos.core.config import get_settings
from analystos.core.logging import get_logger
from analystos.runtime import engine

log = get_logger(__name__)
_loop: asyncio.AbstractEventLoop | None = None
_client = None
_lock = threading.Lock()


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


def _require_temporal() -> None:
    from analystos.core.profiles import require_extra

    require_extra("temporal", "the Temporal orchestrator (ANALYSTOS_ORCHESTRATOR=temporal)")


def workflow_id(run_id: str) -> str:
    return f"analysis-{run_id}"


def start_run(run_id: str) -> str:
    settings = get_settings()
    if settings.orchestrator == "temporal":
        _require_temporal()

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
    local_runtime().submit(run_id)
    return f"local-{run_id}"


def signal_run(run_id: str) -> None:
    """Something the run waits on changed (an approval, a control, feedback). Temporal gets a `nudge`;
    the local orchestrator re-drives the run, which holds no thread while it waits (ADR-0025)."""
    if get_settings().orchestrator != "temporal":
        local_runtime().submit(run_id)
        return

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


# ------------------------------------------------------------------------------ local orchestrator
MAX_ATTEMPTS = 3
PARKED = ("WAITING_USER", "PAUSED")


def _execute_with_retries(run_id: str, key: str, sleep: Callable[[float], None] = time.sleep) -> None:
    for _attempt in range(MAX_ATTEMPTS):
        try:
            engine.execute_task(run_id, key)
            return
        except Exception as exc:  # retried like a Temporal activity
            log.warning("local task %s attempt failed: %s", key, exc)
            sleep(0.5)


class LocalRuntime:
    """The local orchestrator of one API process (ADR-0025).

    * **Drivers** run the Temporal workflow's control loop, one short-lived thread per active run. A
      run that waits for a person (WAITING_USER) or is paused *parks*: its driver returns and no thread
      is held, however long the wait. `submit` (start, the approval signal, a control, the sweep)
      re-drives it; a signal that lands while the driver is deciding to park is not lost (`_nudged`).
    * **Tasks** of every run execute on one bounded pool of `ANALYSTOS_LOCAL_WORKERS` threads, so ready
      tasks run in parallel up to that bound. Without a pool size (the standard profile, tests) each
      driver executes its ready tasks inline, one at a time, as before.
    * **Resume**: `resume()` re-drives every non-terminal run from its Postgres task state after a
      restart; the claims the dead process held are released first (engine.release_orphaned_claims).
    """

    def __init__(self, workers: int | None = None, *, poll: float = 0.3,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.workers = workers
        self.poll = poll
        self.clock, self.sleep = clock, sleep
        self._tasks = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="local-task") if workers else None
        self._lock = threading.Lock()
        self._drivers: dict[str, threading.Thread] = {}
        self._nudged: set[str] = set()
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    # ---------------------------------------------------------------- driving
    def submit(self, run_id: str) -> None:
        """Drive the run now. Idempotent: a run that already has a driver is nudged, not doubled."""
        with self._lock:
            active = self._drivers.get(run_id)
            if active is not None and active.is_alive():
                self._nudged.add(run_id)
                return
            thread = threading.Thread(target=self._drive, args=(run_id,), daemon=True, name=f"run-{run_id}")
            self._drivers[run_id] = thread
        thread.start()

    def active(self) -> list[str]:
        """Runs holding a driver thread right now (a parked run holds none)."""
        with self._lock:
            return [r for r, t in self._drivers.items() if t.is_alive() and t is not threading.current_thread()]

    def _release(self, run_id: str) -> None:
        if self._drivers.get(run_id) is threading.current_thread():
            del self._drivers[run_id]

    def _drive(self, run_id: str) -> None:
        try:
            while True:
                outcome = self.drive(run_id, park=True)
                with self._lock:
                    if run_id in self._nudged and outcome in PARKED:
                        self._nudged.discard(run_id)  # something changed while it was deciding to park
                        continue
                    self._nudged.discard(run_id)
                    self._release(run_id)
                    return
        except Exception:
            log.exception("local driver of %s crashed; the next signal, sweep or restart re-drives it", run_id)
            with self._lock:
                self._release(run_id)

    def drive(self, run_id: str, *, park: bool, max_seconds: float | None = None, poll: float | None = None) -> str:
        """The control loop of the Temporal workflow. With `park`, return WAITING_USER or PAUSED instead
        of polling a run that waits for a person. `max_seconds` bounds only the time spent not waiting."""
        poll = self.poll if poll is None else poll
        engine.plan_run(run_id)
        busy, last = 0.0, self.clock()
        inflight: dict[str, Future] = {}
        while True:
            state = engine.get_state(run_id)
            now = self.clock()
            waiting = bool(state.get("waiting_user")) or state.get("control") == "pause"
            if not waiting:
                busy += now - last
            last = now
            if state.get("terminal"):
                self._drain(inflight)
                return state.get("status", "COMPLETED")
            if state.get("control") == "cancel":
                self._drain(inflight)
                engine.finish_run(run_id, "CANCELLED")
                return "CANCELLED"
            if max_seconds is not None and busy >= max_seconds:
                self._drain(inflight)
                engine.finish_run(run_id, "FAILED", "local orchestrator timeout")
                return "FAILED"
            if state.get("needs_plan"):
                engine.plan_run(run_id)
                continue
            if state.get("fail"):
                self._drain(inflight)
                engine.finish_run(run_id, "FAILED", state["fail"])
                return "FAILED"
            if state.get("done"):
                self._drain(inflight)
                engine.finish_run(run_id, "COMPLETED")
                return "COMPLETED"
            for key in [k for k, f in inflight.items() if f.done()]:
                del inflight[key]
            if park and waiting and not inflight:
                return "PAUSED" if state.get("control") == "pause" else "WAITING_USER"
            ready = [k for k in state.get("ready") or [] if k not in inflight]
            if ready and self._tasks is None:
                for key in ready:
                    _execute_with_retries(run_id, key, self.sleep)
                continue
            for key in ready:
                inflight[key] = self._tasks.submit(_execute_with_retries, run_id, key, self.sleep)
            if inflight:
                wait(list(inflight.values()), timeout=poll, return_when=FIRST_COMPLETED)
            else:
                self.sleep(poll)

    @staticmethod
    def _drain(inflight: dict[str, Future]) -> None:
        """Tasks already started finish (and their results land) before the run is finished."""
        if inflight:
            wait(list(inflight.values()))

    # ---------------------------------------------------------------- restart and sweep
    def resume(self, run_ids: list[str] | None = None) -> list[str]:
        """Re-drive every non-terminal run (or those of `run_ids`) after a restart (idempotent)."""
        run_ids = engine.release_orphaned_claims(run_ids)
        for run_id in run_ids:
            self.submit(run_id)
        if run_ids:
            log.info("local orchestrator resumed %d run(s): %s", len(run_ids), ", ".join(run_ids[:20]))
        return run_ids

    def sweep(self) -> list[str]:
        """Re-drive non-terminal runs without a driver: a parked run whose approval was decided by another
        process (no signal reached this one), or a run whose driver crashed."""
        driving = set(self.active())
        run_ids = [r for r in engine.nonterminal_runs() if r not in driving]
        for run_id in run_ids:
            self.submit(run_id)
        return run_ids

    def start_sweeper(self, seconds: float) -> None:
        if seconds <= 0 or self._sweeper is not None:
            return

        def loop() -> None:
            while not self._stop.wait(seconds):
                try:
                    self.sweep()
                except Exception as exc:  # the database may be briefly unavailable
                    log.warning("local sweep failed: %s", exc)

        self._sweeper = threading.Thread(target=loop, daemon=True, name="local-sweeper")
        self._sweeper.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._tasks is not None:
            self._tasks.shutdown(wait=False, cancel_futures=True)


_runtime: LocalRuntime | None = None
_runtime_lock = threading.Lock()


def local_runtime() -> LocalRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = LocalRuntime(get_settings().local_worker_count)
        return _runtime


def reset_local_runtime(runtime: LocalRuntime | None = None) -> LocalRuntime | None:
    """Swap the process runtime (a restart in tests, new settings); returns the previous one."""
    global _runtime
    with _runtime_lock:
        previous, _runtime = _runtime, runtime
    return previous


def start_local_runtime() -> LocalRuntime | None:
    """API startup: with the local orchestrator and resume on (lite), re-drive non-terminal runs and
    start the sweeper. None when Temporal orchestrates."""
    settings = get_settings()
    if settings.orchestrator == "temporal" or not settings.resume_local_runs:
        return None
    runtime = local_runtime()
    try:
        runtime.resume()
    except Exception as exc:  # e.g. a database that is not migrated yet must not stop the API
        log.warning("local resume skipped: %s", exc)
    runtime.start_sweeper(settings.local_sweep_seconds)
    return runtime


def run_local(run_id: str, *, poll: float = 0.3, max_seconds: float | None = 3600) -> str:
    """Drive a run to a terminal state in the calling thread (tests, scripts). A wait for a person is
    polled here, not parked, and does not count towards `max_seconds`."""
    return local_runtime().drive(run_id, park=False, max_seconds=max_seconds, poll=poll)
