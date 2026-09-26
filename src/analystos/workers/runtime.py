"""The isolated worker's supervisor (ADR-0022, P7-06): one `TaskDispatch` in, one `TaskResult` out.

Transport-independent: the stdio loop (`workers/main.py`, the local subprocess pool) and the Temporal
activity (`workers/temporal.py`) both call `execute`. It checks the capability hash, downloads the
input artifacts with the task token (verifying each hash), runs the job in a child under the task's
budget, uploads the declared outputs with the same token, and reports events. Every failure becomes a
structured error; the supervisor itself never dies with a task.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analystos.contracts.worker import ArtifactRef, TaskDispatch, TaskEvent, TaskResult, TaskUsage, WorkerError
from analystos.core.errors import (
    AnalystOSError,
    BudgetExceeded,
    Conflict,
    EgressBlocked,
    InvalidInput,
    WorkerTaskFailed,
    WorkerUnavailable,
    error_from_dict,
)
from analystos.workers.handlers import EVENT_MARK, RESULT_MARK, handler_for, handler_hash
from analystos.workers.isolation import EgressRefused, job_rlimits, network_namespace_preexec
from analystos.workers.store import sha256

MAX_INLINE_RESULT = 64 * 1024  # larger results must be artifacts
_STDERR_TAIL = 4000


@dataclass
class WorkerConfig:
    pools: tuple[str, ...]
    artifact_url: str
    conformance: bool = False
    scratch_root: str | None = None
    netns: bool = True
    python: str = field(default_factory=lambda: sys.executable)


class StoreClient:
    """The worker's only outbound client: the artifact store, authenticated by the task token."""

    def __init__(self, base_url: str, *, timeout: float = 60.0, transport: Any = None) -> None:
        import httpx

        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, url: str, token: str, **kw: Any) -> Any:
        import httpx

        headers = {"Authorization": f"Bearer {token}", **kw.pop("headers", {})}
        try:
            resp = self._http.request(method, url, headers=headers, **kw)
        except EgressRefused as exc:
            raise EgressBlocked(str(exc)) from None
        except httpx.HTTPError as exc:
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, EgressRefused):
                raise EgressBlocked(str(cause)) from None
            raise WorkerUnavailable(f"the artifact store is unreachable: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            try:
                err = resp.json()["error"]
            except Exception:  # noqa: BLE001
                err = {"code": "upstream_unavailable" if resp.status_code >= 500 else "internal_error",
                       "message": f"artifact store answered {resp.status_code}"}
            raise error_from_dict(err)
        return resp

    def read(self, ref: ArtifactRef, token: str) -> bytes:
        data = self._call("GET", f"/api/worker/artifacts/{ref.artifact_id}", token).content
        if sha256(data) != ref.content_hash or len(data) != ref.bytes:
            raise Conflict(f"input {ref.artifact_id} does not match its reference; refused",
                           details={"artifact_id": ref.artifact_id})
        return data

    def write(self, task_id: str, name: str, data: bytes, *, kind: str, media_type: str, token: str) -> tuple[ArtifactRef, bool]:
        body = self._call("PUT", f"/api/worker/tasks/{task_id}/outputs/{name}", token, content=data,
                          headers={"Content-Type": media_type, "X-Artifact-Kind": kind,
                                   "X-Content-SHA256": sha256(data)}).json()
        return ArtifactRef.model_validate(body["ref"]), bool(body["created"])

    def model(self, purpose: str, messages: list[dict[str, str]], token: str, *, max_tokens: int | None = None) -> dict:
        """The narrow model callback (ADR-0022 decision 4): the control plane's router, the token's purposes."""
        return self._call("POST", "/api/worker/model", token,
                          json={"purpose": purpose, "messages": messages, "max_tokens": max_tokens}).json()


def child_environment(scratch: Path) -> dict[str, str]:
    """The job's whole environment: no credentials, no proxies, single-threaded maths libraries."""
    import analystos

    package_root = str(Path(analystos.__file__).resolve().parents[1])
    paths = [package_root, *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p != package_root]]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(scratch), "TMPDIR": str(scratch / "tmp"),
           "LANG": os.environ.get("LANG", "C.UTF-8"), "PYTHONPATH": os.pathsep.join(paths), "PYTHONHASHSEED": "0",
           "PYTHONDONTWRITEBYTECODE": "1"}
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[var] = "1"
    return env


@dataclass
class ChildOutcome:
    body: dict[str, Any] | None
    returncode: int
    cpu_seconds: float
    max_rss_mb: float
    wall_seconds: float
    killed_wall: bool
    stderr_tail: str


def run_child(job: dict[str, Any], budget: dict[str, Any], scratch: Path, config: WorkerConfig,
              on_progress: Callable[[dict[str, Any]], None]) -> ChildOutcome:
    preexec = network_namespace_preexec(job_rlimits(budget), try_netns=config.netns and sys.platform.startswith("linux"))
    started = time.monotonic()
    import subprocess

    proc = subprocess.Popen([config.python, "-m", "analystos.workers.child"], stdin=subprocess.PIPE,  # noqa: S603
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(scratch),
                            env=child_environment(scratch), preexec_fn=preexec, close_fds=True)
    body: dict[str, Any] = {}
    err_tail: list[bytes] = []

    def read_stdout() -> None:
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith(RESULT_MARK):
                try:
                    body["result"] = json.loads(line[len(RESULT_MARK):])
                except ValueError:
                    pass
            elif line.startswith(EVENT_MARK):
                try:
                    on_progress(json.loads(line[len(EVENT_MARK):]))
                except Exception:  # noqa: BLE001 - a malformed progress line is dropped
                    pass

    def read_stderr() -> None:
        size = 0
        for raw in proc.stderr:  # type: ignore[union-attr]
            err_tail.append(raw)
            size += len(raw)
            while size > _STDERR_TAIL and len(err_tail) > 1:
                size -= len(err_tail.pop(0))

    status: dict[str, Any] = {}

    def wait() -> None:
        _, st, ru = os.wait4(proc.pid, 0)
        status.update(code=os.waitstatus_to_exitcode(st), ru=ru)

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    waiter = threading.Thread(target=wait, daemon=True)
    for t in readers:
        t.start()
    try:
        proc.stdin.write(json.dumps(job).encode())  # type: ignore[union-attr]
        proc.stdin.close()  # type: ignore[union-attr]
    except BrokenPipeError:
        pass
    waiter.start()
    waiter.join(timeout=float(budget["wall_seconds"]))
    killed = waiter.is_alive()
    if killed:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        waiter.join(timeout=30)
    for t in readers:
        t.join(timeout=5)
    proc.returncode = status.get("code", -signal.SIGKILL)
    ru = status.get("ru")
    return ChildOutcome(body=body.get("result"), returncode=proc.returncode,
                        cpu_seconds=(ru.ru_utime + ru.ru_stime) if ru else 0.0,
                        max_rss_mb=(ru.ru_maxrss / 1024) if ru else 0.0, wall_seconds=time.monotonic() - started,
                        killed_wall=killed, stderr_tail=b"".join(err_tail).decode("utf-8", "replace")[-_STDERR_TAIL:])


def classify(out: ChildOutcome, budget: dict[str, Any]) -> AnalystOSError | None:
    """The error a finished child stands for, or None when it succeeded."""
    if out.killed_wall:
        return BudgetExceeded(f"the job exceeded its wall-clock budget ({budget['wall_seconds']} s) and was killed",
                              details={"limit": "wall", "wall_seconds": budget["wall_seconds"]})
    if out.body is not None:
        return None if out.body.get("ok") else error_from_dict(out.body.get("error") or {})
    sig = -out.returncode if out.returncode < 0 else None
    if sig in (signal.SIGXCPU, signal.SIGKILL) and out.cpu_seconds >= float(budget["cpu_seconds"]) - 0.5:
        return BudgetExceeded(f"the job exceeded its CPU budget ({budget['cpu_seconds']} s) and was killed",
                              details={"limit": "cpu", "cpu_seconds": budget["cpu_seconds"],
                                       "used": round(out.cpu_seconds, 2)})
    if sig == signal.SIGXFSZ:
        return BudgetExceeded("the job wrote more than its max_output_bytes", details={"limit": "output_bytes"})
    if "MemoryError" in out.stderr_tail or "std::bad_alloc" in out.stderr_tail or "Cannot allocate memory" in out.stderr_tail:
        return BudgetExceeded("the job ran out of its memory budget", details={"limit": "memory",
                                                                              "memory_mb": budget["memory_mb"]})
    return WorkerTaskFailed(f"the job ended without a result (exit {out.returncode})",
                            details={"returncode": out.returncode, "stderr": out.stderr_tail[-1000:]})


def execute(dispatch: TaskDispatch, config: WorkerConfig, *, emit: Callable[[TaskEvent], None] | None = None,
            client: StoreClient | None = None) -> TaskResult:
    env = dispatch.envelope
    events: list[TaskEvent] = []
    lock = threading.Lock()

    def event(type_: str, **data: Any) -> None:
        with lock:
            e = TaskEvent(task_id=env.task_id, type=type_, seq=len(events), at=time.time(), data=data)
            events.append(e)
        if emit is not None:
            try:
                emit(e)
            except Exception:  # noqa: BLE001 - reporting never fails the task
                pass

    own_client = client is None
    client = client or StoreClient(config.artifact_url)
    usage = TaskUsage()
    scratch: Path | None = None
    outputs: dict[str, ArtifactRef] = {}
    result: dict[str, Any] = {}
    error: WorkerError | None = None
    event("task.started", pool=dispatch.pool, capability=env.capability.id, kind=env.spec.kind)
    try:
        if dispatch.pool not in config.pools:
            raise InvalidInput(f"this worker serves {', '.join(config.pools)}, not {dispatch.pool}")
        handler_for(env.spec.kind, dispatch.pool, conformance=config.conformance)
        if handler_hash(env.spec.kind) != env.capability.content_hash:
            raise Conflict(f"the {dispatch.pool} worker runs a different version of '{env.spec.kind}' than the control "
                           "plane asked for; deploy the same build", details={"capability": env.capability.id})
        if env.deadline is not None and env.deadline.timestamp() < time.time():
            raise BudgetExceeded("the task's deadline has passed", details={"limit": "deadline"})
        scratch = Path(tempfile.mkdtemp(prefix="aos-task-", dir=config.scratch_root))
        (scratch / "in").mkdir()
        (scratch / "out").mkdir()
        (scratch / "tmp").mkdir()
        inputs: dict[str, str] = {}
        for ref in env.input_artifacts:
            path = scratch / "in" / ref.artifact_id
            path.write_bytes(client.read(ref, dispatch.token))
            inputs[ref.artifact_id] = str(path)
        budget = env.budget.model_dump()
        job = {"task_id": env.task_id, "pool": dispatch.pool, "kind": env.spec.kind, "spec": env.spec.model_dump(mode="json"),
               "inputs": inputs, "out_dir": str(scratch / "out"), "outputs": env.required_outputs, "budget": budget,
               "scratch": str(scratch), "conformance": config.conformance,
               "envelope": env.model_dump(mode="json")}
        out = run_child(job, budget, scratch, config,
                        lambda p: event("task.progress", **(p.get("data") or {})))
        usage = TaskUsage(cpu_seconds=round(out.cpu_seconds, 3), wall_seconds=round(out.wall_seconds, 3),
                          max_rss_mb=round(out.max_rss_mb, 1), network=(out.body or {}).get("network", "none"))
        failure = classify(out, budget)
        if failure is not None:
            raise failure
        written = out.body.get("outputs") or {}
        missing = [n for n in env.required_outputs if n not in written]
        if missing:
            raise InvalidInput(f"the job did not produce required output(s) {', '.join(missing)}", details={"missing": missing})
        files = {n: (scratch / "out" / n).read_bytes() for n in env.required_outputs}
        total = sum(len(b) for b in files.values())
        usage.output_bytes = total
        if total > env.budget.max_output_bytes:
            raise BudgetExceeded(f"the job's outputs ({total} bytes) exceed its max_output_bytes ({env.budget.max_output_bytes})",
                                 details={"limit": "output_bytes", "bytes": total})
        result = out.body.get("result") or {}
        if len(json.dumps(result, default=str)) > MAX_INLINE_RESULT:
            raise InvalidInput("the job's inline result is too large; large results must be output artifacts")
        for name in env.required_outputs:
            outputs[name], _ = client.write(env.task_id, name, files[name], kind=written[name]["kind"],
                                            media_type=written[name]["media_type"], token=dispatch.token)
        event("task.completed", outputs={n: r.artifact_id for n, r in outputs.items()}, usage=usage.model_dump())
    except AnalystOSError as exc:
        error = WorkerError(**exc.to_dict())
    except Exception as exc:  # noqa: BLE001 - a supervisor bug is still a structured failure
        error = WorkerError(code="internal_error", message=f"{type(exc).__name__}: {str(exc)[:500]}")
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        if own_client:
            client.close()
    if error is not None:
        outputs = {}
        event("task.failed", error=error.model_dump())
    return TaskResult(task_id=env.task_id, idempotency_key=env.idempotency_key,
                      status="failed" if error else "completed", outputs=outputs, result=result if not error else {},
                      error=error, usage=usage, events=list(events))
