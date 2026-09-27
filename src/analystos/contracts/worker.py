"""Compute-worker protocol (ADR-0022, P7-06): what the control plane hands an isolated pool and what comes back.

A `TaskEnvelope` names one unit of heavy work: which capability (and the exact code version, by hash),
its typed spec, the immutable input artifacts, the budget the pool enforces, and the outputs it must
produce. It travels with a scoped capability token (`TaskDispatch`), never with a database or provider
credential. Workers return `ArtifactRef`s and a small structured result or a structured error whose
`code` is a `core/errors.py` code, never large inline payloads.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analystos.core.ids import stable_hash

ISOLATED_POOLS = ("compute-py", "compute-ml")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")  # output names: a file name on the worker, a path segment on the store
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactRef(_Strict):
    """An immutable artifact in the store. Content-addressed: the same bytes in a workspace are the same artifact."""

    artifact_id: str
    version: int = 1
    kind: str
    content_hash: str  # sha256 of the bytes
    media_type: str = "application/octet-stream"
    bytes: int = Field(ge=0)

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, v: str) -> str:
        if not _HEX64.match(v):
            raise ValueError("content_hash must be a lowercase sha256 hex digest")
        return v


class CapabilityRef(_Strict):
    """The capability and the exact code the worker must run: a worker whose handler hashes differently refuses."""

    id: str
    version: str = "1"
    content_hash: str


class Budget(_Strict):
    """Enforced by the pool, not trusted to the job: CPU and memory by rlimits, wall clock by a kill,
    output bytes by the store and the file-size limit, trials by the job context."""

    cpu_seconds: int = Field(default=300, ge=1, le=86_400)
    memory_mb: int = Field(default=2048, ge=64, le=262_144)
    wall_seconds: int = Field(default=600, ge=1, le=86_400)
    max_output_bytes: int = Field(default=256 * 1024 * 1024, ge=1)
    max_trials: int | None = Field(default=None, ge=1)


# ------------------------------------------------------------------------------------ typed specs
class SnapshotInput(_Strict):
    artifact_id: str
    snapshot: str  # the recipe snapshot id (stable hash of its content), re-checked by the worker

    @field_validator("snapshot")
    @classmethod
    def _snapshot(cls, v: str) -> str:
        if not _HEX64.match(v):
            raise ValueError("snapshot must be a snapshot id")
        return v


class RecipeSnapshotSpec(_Strict):
    """A recipe's DuckDB statement over immutable input snapshots (ADR-0023, P6-04)."""

    kind: Literal["recipe.snapshot"] = "recipe.snapshot"
    sql: str
    tables: dict[str, SnapshotInput]
    max_rows: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)


class MLJobSpec(_Strict):
    """A classical-ML job (ADR-0024, P5-04) for `compute-ml`. `job` is the pure job's input; inputs it
    reads are named by artifact id in `input_artifacts`. P5-04 narrows `job` to its MLSpec."""

    kind: Literal["ml.job"] = "ml.job"
    job: dict[str, Any] = Field(default_factory=dict)


class PythonCellSpec(_Strict):
    """A notebook/step Python cell (P7-12) for `compute-py`: restricted code over the JSON input artifact named
    `inputs_artifact`, under the sandbox's static and runtime policy inside the credential-free worker."""

    kind: Literal["python.cell"] = "python.cell"
    code: str = Field(max_length=100_000)
    inputs_artifact: str
    allowed_imports: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=3600)
    memory_mb: int = Field(default=1024, ge=64, le=65_536)


class ProbeSpec(_Strict):
    """Conformance probe (tests/conformance/worker): served only by a worker started with --conformance."""

    kind: Literal["conformance.probe"] = "conformance.probe"
    action: Literal["echo", "read", "write", "egress", "cpu", "memory", "sleep", "big_output", "raise", "trials",
                    "environment"]
    args: dict[str, Any] = Field(default_factory=dict)


TaskSpec = Annotated[RecipeSnapshotSpec | MLJobSpec | PythonCellSpec | ProbeSpec, Field(discriminator="kind")]


class TaskEnvelope(_Strict):
    task_id: str
    run_id: str | None = None
    workspace_id: str
    work_order_id: str | None = None
    capability: CapabilityRef
    spec: TaskSpec
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    context_ref: str | None = None
    policy_ref: str | None = None
    budget: Budget = Field(default_factory=Budget)
    required_outputs: list[str] = Field(default_factory=list)
    trace_parent: str | None = None
    deadline: datetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("required_outputs")
    @classmethod
    def _outputs(cls, v: list[str]) -> list[str]:
        bad = [n for n in v if not _NAME.match(n)]
        if bad:
            raise ValueError(f"output names must be lower snake case: {bad}")
        if len(set(v)) != len(v):
            raise ValueError("output names must be unique")
        return v

    @property
    def envelope_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class TaskDispatch(_Strict):
    """What a pool receives: the envelope, its pool and the scoped artifact token. The token is the only
    credential a worker ever holds; the artifact store URL comes from the worker's own configuration."""

    pool: Literal["compute-py", "compute-ml"]
    envelope: TaskEnvelope
    token: str


# ------------------------------------------------------------------------------------ results
TaskEventType = Literal["task.started", "task.progress", "task.completed", "task.failed"]


class TaskEvent(_Strict):
    task_id: str
    type: TaskEventType
    seq: int = 0
    at: float = 0.0  # unix seconds on the worker
    data: dict[str, Any] = Field(default_factory=dict)


class WorkerError(_Strict):
    """A `core/errors.py` error as data; `analystos.workers.dispatch.raise_for_result` raises the class back."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class TaskUsage(_Strict):
    cpu_seconds: float = 0.0
    wall_seconds: float = 0.0
    max_rss_mb: float = 0.0
    output_bytes: int = 0
    network: str = "none"  # how the job's egress was refused: namespace (no interfaces but lo) | guard


class TaskResult(_Strict):
    task_id: str
    idempotency_key: str
    status: Literal["completed", "failed"]
    outputs: dict[str, ArtifactRef] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: WorkerError | None = None
    usage: TaskUsage = Field(default_factory=TaskUsage)
    events: list[TaskEvent] = Field(default_factory=list)


class ModelCallbackRequest(_Strict):
    """A worker's narrow model call, routed by the control plane under the token's purposes and budget."""

    purpose: str
    messages: list[dict[str, str]] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)


class ModelCallbackResponse(_Strict):
    text: str
    model: str | None = None
    purpose: str
    calls_left: int
