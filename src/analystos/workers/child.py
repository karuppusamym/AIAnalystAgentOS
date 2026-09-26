"""The job process of an isolated worker (P7-06): one task, under the task's rlimits, no network.

Started by `workers/runtime.py` with a minimal environment, in its own session and (where the kernel
allows) its own empty network namespace. Reads the job as JSON on stdin; writes protocol lines on
stdout (progress events and one result, each behind a marker; anything a job prints goes to stderr).
Every failure leaves as a structured `core/errors.py` error.
"""
from __future__ import annotations

import errno
import json
import os
import signal
import socket
import sys
import time
from typing import Any

from analystos.core.errors import AnalystOSError, BudgetExceeded, EgressBlocked
from analystos.workers.handlers import RESULT_MARK, JobContext, emit_line, handler_for, resolve
from analystos.workers.isolation import EgressRefused, install_egress_guard


def network_mode() -> str:
    try:
        names = sorted(n for _, n in socket.if_nameindex())
    except OSError:
        return "guard"
    return "namespace" if names == ["lo"] else "guard"


def run(job: dict[str, Any]) -> dict[str, Any]:
    ctx = JobContext(task_id=job["task_id"], inputs=job["inputs"], out_dir=job["out_dir"], outputs=job["outputs"],
                     budget=job["budget"], envelope=job["envelope"], scratch=job["scratch"])
    h = handler_for(job["kind"], job["pool"], conformance=bool(job.get("conformance")))
    fn = resolve(h)
    if h.adapter == "pure":
        result = fn({**(job["spec"].get("job") or {}), "inputs": {k: str(v) for k, v in ctx.inputs.items()}})
        ctx.write_output("result", json.dumps(result, sort_keys=True, default=str).encode(), kind="json",
                         media_type="application/json")
    else:
        result = fn(job["spec"], ctx)
    return {"ok": True, "result": result if isinstance(result, dict) else {"value": result}, "outputs": ctx.written}


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, AnalystOSError):
        return exc.to_dict()
    if isinstance(exc, EgressRefused):
        return EgressBlocked(str(exc), details={"refused_by": "worker egress guard"}).to_dict()
    if isinstance(exc, MemoryError):
        return BudgetExceeded("the job ran out of its memory budget", details={"limit": "memory"}).to_dict()
    if isinstance(exc, OSError) and exc.errno == errno.EFBIG:
        return BudgetExceeded("the job wrote more than its max_output_bytes", details={"limit": "output_bytes"}).to_dict()
    return {"code": "internal_error", "message": f"{type(exc).__name__}: {str(exc)[:500]}", "retryable": False,
            "details": {"type": type(exc).__name__}}


def main() -> int:
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past RLIMIT_FSIZE fails with EFBIG instead of killing us
    sys.stdout = sys.stderr  # a job's prints never corrupt the protocol stream
    install_egress_guard(())
    started = time.monotonic()
    net = network_mode()
    try:
        job = json.loads(sys.stdin.read())
        os.chdir(job["scratch"])
        body = run(job)
    except BaseException as exc:  # noqa: BLE001 - everything leaves as a structured error
        body = {"ok": False, "error": _error(exc)}
    body["network"] = net
    body["seconds"] = round(time.monotonic() - started, 3)
    emit_line(RESULT_MARK, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
