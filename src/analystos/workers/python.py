"""Step and notebook Python cells on the isolated `compute-py` pool (P7-12 on P7-06).

`services/steps.execute_python` sends a cell here when the installation runs `compute-py`; otherwise it
runs in the sandbox as before. The cell's inputs (upstream step results) go to the artifact store as
one JSON artifact; the worker applies the sandbox's static policy, then runs the sandbox harness (the
same runtime allowlist and restricted builtins) as a grandchild of its job process: no credentials, no
database, no connection of any kind, the job's rlimits and, where the kernel allows, an empty network
namespace. The cell's full outcome comes back as a JSON output artifact; the returned dict has the same
shape as the sandbox path, with `isolation: "compute-py"`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from analystos.contracts.worker import PythonCellSpec, TaskEnvelope
from analystos.core.errors import Conflict
from analystos.core.ids import new_id, stable_hash
from analystos.workers.dispatch import capability_ref, default_budget, dispatch_isolated, raise_for_result
from analystos.workers.store import ArtifactStore, control_store

POOL = "compute-py"
PLATFORM_WORKSPACE = "_platform"
ISOLATION = "compute-py"
_KEYS = ("ok", "result", "error", "stdout", "duration_ms", "timed_out", "isolation", "network_isolated")


class _SandboxSettings:
    """What the sandbox harness launcher reads from settings; an isolated worker never builds Settings."""

    sandbox_scratch_mb = 64
    sandbox_container_startup_seconds = 0
    sandbox_pids_limit = 64
    sandbox_masked_paths: list[str] = []


# ------------------------------------------------------------------------------------ worker side
def python_cell(spec: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The `python.cell` handler: the sandbox's policy check, then its harness under the job's limits."""
    from analystos.sandbox.runner import _execute, check_code
    from analystos.workers.child import network_mode

    inputs = json.loads(ctx.read_input(spec["inputs_artifact"]) or b"{}")
    allowed = tuple(spec.get("allowed_imports") or ())
    net = network_mode() == "namespace"
    problems = check_code(spec["code"], allowed)
    if problems:
        out = {"ok": False, "result": None, "error": "rejected by sandbox policy: " + "; ".join(problems[:10]),
               "stdout": "", "duration_ms": 0, "timed_out": False}
    else:
        ctx.progress("policy checked", 0.1)
        r = _execute(spec["code"], backend="none", inputs=inputs, timeout_s=float(spec["timeout_seconds"]),
                     memory_mb=int(spec["memory_mb"]), allowed_imports=allowed, settings=_SandboxSettings())
        out = {"ok": r.ok, "result": r.result, "error": r.error, "stdout": r.stdout[-4000:],
               "duration_ms": r.duration_ms, "timed_out": r.timed_out}
    out.update(isolation=ISOLATION, network_isolated=net)
    body = json.dumps(out, sort_keys=True, default=str).encode()
    ctx.write_output("result", body, kind="python_cell_result", media_type="application/json")
    return {"ok": out["ok"], "result_sha256": hashlib.sha256(body).hexdigest()}


# ------------------------------------------------------------------------------------ control-plane side
def cell_envelope(code: str, inputs: dict[str, Any], *, allowed_imports: tuple[str, ...], timeout_s: float,
                  memory_mb: int, artifacts: ArtifactStore, workspace_id: str | None = None) -> TaskEnvelope:
    from analystos.sandbox.runner import _json_default

    workspace_id = workspace_id or PLATFORM_WORKSPACE
    data = json.dumps(inputs or {}, sort_keys=True, default=_json_default).encode()
    ref = artifacts.put(workspace_id, data, kind="python_cell_inputs", media_type="application/json")
    spec = PythonCellSpec(code=code, inputs_artifact=ref.artifact_id, allowed_imports=list(allowed_imports),
                          timeout_seconds=timeout_s, memory_mb=memory_mb)
    capability = capability_ref("python.cell", "python.cell")
    budget = default_budget(POOL)
    # the harness is a separate process with its own limits; the job's limits bound the supervisor around it
    budget = budget.model_copy(update={"wall_seconds": max(int(timeout_s) + 60, 60),
                                       "cpu_seconds": max(int(timeout_s) + 30, 30),
                                       "memory_mb": max(budget.memory_mb, int(memory_mb) + 512),
                                       "max_output_bytes": min(budget.max_output_bytes, 32 * 1024 * 1024)})
    key = stable_hash({"capability": capability.model_dump(), "spec": spec.model_dump(), "call": new_id("pycall")})[:48]
    return TaskEnvelope(task_id=new_id("wtask"), workspace_id=workspace_id, capability=capability, spec=spec,
                        input_artifacts=[ref], budget=budget, required_outputs=["result"], context_ref="python.cell",
                        idempotency_key=key)


def run_python_isolated(code: str, inputs: dict[str, Any], *, allowed_imports: tuple[str, ...], timeout_s: float = 30,
                        memory_mb: int = 1024, workspace_id: str | None = None, transport: Any = None, sink: Any = None,
                        settings: Any = None, secret: bytes | None = None,
                        artifacts: ArtifactStore | None = None) -> dict[str, Any]:
    """`execute_python` on the isolated `compute-py` pool. The cell's own failures (policy, exception, its
    timeout) are `ok: False` as in the sandbox; a pool failure raises, never falls back to the sandbox."""
    artifacts = artifacts or control_store(settings)
    envelope = cell_envelope(code, inputs, allowed_imports=allowed_imports, timeout_s=timeout_s, memory_mb=memory_mb,
                             artifacts=artifacts, workspace_id=workspace_id)
    result = raise_for_result(dispatch_isolated(POOL, envelope, transport=transport, sink=sink, settings=settings,
                                                secret=secret))
    _, body = artifacts.read(result.outputs["result"].artifact_id)
    if hashlib.sha256(body).hexdigest() != result.result.get("result_sha256"):
        raise Conflict("the worker's cell result does not match the hash it reported; refused")
    out = json.loads(body)
    return {k: out.get(k) for k in _KEYS}
