"""Which job each isolated pool runs, and the context a job gets (ADR-0022, P7-06).

A handler is `module:function`. Two shapes:

* `context`: `fn(spec: dict, ctx: JobContext) -> dict` reads inputs and writes outputs through `ctx`;
* `pure`: `fn(job: dict) -> dict`, a pure job (P5-04 `run_ml_job`). The worker passes
  `{**spec["job"], "inputs": {artifact_id: local path}}` and stores the returned dict as the JSON
  output `result` (so the envelope must list `required_outputs: ["result"]`).

The capability hash an envelope carries is the SHA-256 of the handler module's source: a worker whose
code differs from the control plane's refuses the task instead of running a different version.

Plugging in an ML job (P5-04): `register_handler("ml.job", "analystos.ml.jobs:run_ml_job", pools=("compute-ml",),
adapter="pure")` (or change `ML_JOB_TARGET`), build a `TaskEnvelope` with `spec=MLJobSpec(job=...)` and call
`analystos.workers.dispatch.dispatch_isolated("compute-ml", envelope)`. Nothing else in the protocol changes.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analystos.core.errors import BudgetExceeded, FeatureUnavailable, InvalidInput, UnsupportedCapability

ML_JOB_TARGET = "analystos.ml.jobs:run_ml_job"
EVENT_MARK = "\x1eAOS-EVENT "
RESULT_MARK = "\x1eAOS-RESULT "


@dataclass(frozen=True)
class Handler:
    kind: str
    target: str
    pools: tuple[str, ...]
    adapter: str = "context"  # context | pure
    version: str = "1"
    conformance_only: bool = False

    @property
    def module(self) -> str:
        return self.target.split(":", 1)[0]


HANDLERS: dict[str, Handler] = {}


def register_handler(kind: str, target: str, *, pools: tuple[str, ...], adapter: str = "context", version: str = "1",
                     conformance_only: bool = False) -> Handler:
    if adapter not in ("context", "pure"):
        raise ValueError("adapter must be context or pure")
    HANDLERS[kind] = Handler(kind, target, tuple(pools), adapter, version, conformance_only)
    return HANDLERS[kind]


register_handler("recipe.snapshot", "analystos.workers.handlers:recipe_snapshot", pools=("compute-py",))
register_handler("ml.job", ML_JOB_TARGET, pools=("compute-ml",), adapter="pure")
register_handler("conformance.probe", "analystos.workers.conformance:probe", pools=("compute-py", "compute-ml"),
                 conformance_only=True)


def handler_for(kind: str, pool: str, *, conformance: bool = False) -> Handler:
    h = HANDLERS.get(kind)
    if h is None or pool not in h.pools or (h.conformance_only and not conformance):
        raise UnsupportedCapability(f"the {pool} pool does not run '{kind}' jobs",
                                    details={"pool": pool, "kind": kind, "serves": served_by(pool, conformance)})
    return h


def served_by(pool: str, conformance: bool = False) -> list[str]:
    return sorted(k for k, h in HANDLERS.items() if pool in h.pools and (conformance or not h.conformance_only))


def handler_hash(kind: str) -> str:
    """SHA-256 of the handler's kind, version and module source (without importing it)."""
    h = HANDLERS.get(kind)
    if h is None:
        raise UnsupportedCapability(f"no isolated handler for '{kind}'")
    try:
        spec = importlib.util.find_spec(h.module)
    except (ImportError, ValueError):
        spec = None
    if spec is None or not spec.origin or not Path(spec.origin).exists():
        raise FeatureUnavailable(f"'{kind}' jobs are unavailable: {h.module} is not installed here",
                                 details={"kind": kind, "module": h.module})
    digest = hashlib.sha256(f"{h.kind}\x00{h.version}\x00".encode())
    digest.update(Path(spec.origin).read_bytes())
    return digest.hexdigest()


def resolve(h: Handler) -> Any:
    module, _, attr = h.target.partition(":")
    return getattr(importlib.import_module(module), attr)


# ------------------------------------------------------------------------------------ job context
class JobContext:
    """What a job may touch: its downloaded inputs, its declared outputs (files the supervisor uploads),
    progress events and the trial budget. No network, no credentials."""

    def __init__(self, *, task_id: str, inputs: dict[str, str], out_dir: str, outputs: list[str], budget: dict[str, Any],
                 envelope: dict[str, Any], scratch: str) -> None:
        self.task_id, self.budget, self.envelope = task_id, budget, envelope
        self.inputs = {k: Path(v) for k, v in inputs.items()}
        self.out_dir, self.scratch = Path(out_dir), Path(scratch)
        self.declared = list(outputs)
        self.written: dict[str, dict[str, str]] = {}
        self.trials = 0

    def input_path(self, artifact_id: str) -> Path:
        if artifact_id not in self.inputs:
            raise InvalidInput(f"artifact {artifact_id} is not an input of this task")
        return self.inputs[artifact_id]

    def read_input(self, artifact_id: str) -> bytes:
        return self.input_path(artifact_id).read_bytes()

    def write_output(self, name: str, data: bytes, *, kind: str, media_type: str = "application/octet-stream") -> None:
        if name not in self.declared:
            raise InvalidInput(f"output '{name}' is not declared by the task envelope", details={"declared": self.declared})
        (self.out_dir / name).write_bytes(data)
        self.written[name] = {"kind": kind, "media_type": media_type}

    def progress(self, message: str, fraction: float | None = None, **data: Any) -> None:
        emit_line(EVENT_MARK, {"type": "task.progress", "at": time.time(),
                               "data": {"message": message, "fraction": fraction, **data}})

    def trial(self) -> int:
        """Count one search trial; past `max_trials` the job is stopped with budget_exceeded."""
        self.trials += 1
        limit = self.budget.get("max_trials")
        if limit is not None and self.trials > int(limit):
            raise BudgetExceeded(f"the task's max_trials ({limit}) is spent", details={"limit": "trials", "max_trials": limit})
        return self.trials


def emit_line(mark: str, body: dict[str, Any]) -> None:
    sys.__stdout__.write(mark + json.dumps(body, default=str) + "\n")
    sys.__stdout__.flush()


# ------------------------------------------------------------------------------------ handlers
def recipe_snapshot(spec: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """The recipe snapshot job (P6-04) unchanged, over snapshot files placed in the job's scratch store.
    `run_snapshot_job` re-verifies every snapshot against its content hash before DuckDB sees it."""
    from analystos.recipes.execute import SnapshotStore, run_snapshot_job

    root = ctx.scratch / "snapshots"
    store = SnapshotStore(root / "recipe_snapshots")
    tables: dict[str, str] = {}
    for asset, entry in sorted(spec["tables"].items()):
        path = store.path(entry["snapshot"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ctx.read_input(entry["artifact_id"]))
        tables[asset] = entry["snapshot"]
    ctx.progress("inputs verified", 0.2, tables=len(tables))
    out = run_snapshot_job({"sql": spec["sql"], "tables": tables, "max_rows": spec["max_rows"],
                            "timeout_seconds": spec["timeout_seconds"], "artifact_dir": str(root)})
    ctx.write_output("result", store.path(out["result"]).read_bytes(), kind="recipe_snapshot",
                     media_type="application/gzip")
    return out
