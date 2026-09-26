"""Steps and branches: the Data Thread (spec v4 §7, P7-04/05/12).

Every run, Ask thread and notebook is a sequence of steps:
`Step {id, version, kind, spec, receipts[], result_snapshot: ArtifactRef, chart_spec?, checks[], verification_record?}`.
A step's identity is stable; each execution is a new version. Editing a step creates `version + 1`,
re-runs it and the steps that depend on it and voids their verdicts (ADR-0020); older versions stay
readable. A branch forks from any step and keeps its parent pointer.

Step specs by kind (validated in `services/steps.validate_spec`):

* ``query``  ``{"sql": str, "source_id"?: str}`` or ``{"semantic_query": SemanticQuery}``; notebook cells add ``"cell": "sql"``;
* ``method`` ``{"analysis_spec": AnalysisSpec}`` or ``{"cell": "python", "code": str}`` (restricted Python on upstream results);
* ``chart``  ``{"chart": {"type", "x", "y"}}`` over the one step it depends on;
* ``claim``  ``{"text": str}`` (markdown; every number must bind to an upstream result);
* ``plan``   ``{"text": str}`` or a run's plan; recorded, not executed;
* ``recipe`` / ``train`` recorded references (their executors are P6 recipes and P5 ML).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StepKind = Literal["plan", "query", "method", "recipe", "train", "chart", "claim"]
StepStatus = Literal["pending", "ok", "flagged", "failed", "unsupported", "recorded"]
ContainerType = Literal["run", "ask_thread", "notebook"]
CellType = Literal["markdown", "sql", "python"]
STEP_SCHEMA_VERSION = 1


class ArtifactRef(BaseModel):
    """A stored result snapshot. The compute-worker contract (P7-06, `contracts/worker.py`) may widen
    this; the fields here are the ones a step reads (the artifact row, its version and content hash)."""

    model_config = ConfigDict(extra="allow")
    kind: Literal["artifact"] = "artifact"
    id: str
    version: int = 1
    content_hash: str
    media_type: str = "application/json"


class StepCheck(BaseModel):
    check: str
    passed: bool
    severity: Literal["error", "warning", "info"] = "error"
    detail: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    corrected: bool = False  # the check failed, a safe correction was applied and the step re-ran


class Step(BaseModel):
    """One version of a step, as the API returns it."""

    schema_version: Literal[1] = STEP_SCHEMA_VERSION
    id: str
    version: int
    current_version: int
    kind: StepKind
    title: str
    status: StepStatus
    spec: dict[str, Any]
    spec_hash: str
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    result_snapshot: ArtifactRef | None = None
    chart_spec: dict[str, Any] | None = None
    checks: list[StepCheck] = Field(default_factory=list)
    corrections: list[dict[str, Any]] = Field(default_factory=list)
    verification_record: dict[str, Any] | None = None  # evidence.verification.state_of
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    branch_id: str
    container: dict[str, str]
    seq: int
    origin: dict[str, Any] = Field(default_factory=dict)
    forked_from: dict[str, Any] | None = None
    reason: str
    error: str | None = None
    inherited: bool = False  # shown in a fork but owned by its parent branch
    created_by: str | None = None
    created_at: str | None = None


class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: StepKind
    title: str | None = Field(default=None, max_length=300)
    spec: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    chart_spec: dict[str, Any] | None = None


class StepEdit(BaseModel):
    """Edit a step: a new version with this spec (and/or title/chart), then re-run it and its dependents."""

    model_config = ConfigDict(extra="forbid")
    spec: dict[str, Any] | None = None
    title: str | None = Field(default=None, max_length=300)
    chart_spec: dict[str, Any] | None = None


class Branch(BaseModel):
    id: str
    name: str
    container: dict[str, str]
    parent_branch_id: str | None = None
    forked_from: dict[str, Any] | None = None  # {step_id, version}
    base: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["open", "merged"] = "open"
    merged_into: list[str] = Field(default_factory=list)
    created_by: str | None = None
    created_at: str | None = None


class ForkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_step_id: str
    name: str | None = Field(default=None, max_length=200)
    include_downstream: bool = False  # also copy the steps after the fork point (to replay them on an edit)
    spec: dict[str, Any] | None = None  # edit the forked copy at once (a "what if")


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str | None = None  # append to a report an earlier merge created
    title: str | None = Field(default=None, max_length=300)


class PinIn(BaseModel):
    """Pin one step version to a tile or a schedule. Both write outside the step, so both are approvals
    bound to the frozen payload's hash: the first call returns the approval, the second (with the
    approved `approval_id`) pins."""

    model_config = ConfigDict(extra="forbid")
    target: Literal["tile", "schedule"]
    version: int | None = None  # default: the current version
    dashboard: str | None = Field(default=None, max_length=200)
    destination: str | None = None
    name: str | None = Field(default=None, max_length=200)
    cron: str | None = None
    timezone: str = "UTC"
    approval_id: str | None = None


class NotebookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)


class CellIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cell: CellType
    source: str = Field(max_length=100_000)
    title: str | None = Field(default=None, max_length=300)
    depends_on: list[str] | None = None  # default: python cells read every earlier sql/python cell
    source_id: str | None = None  # sql cells on a multi-source scope


class CellEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(max_length=100_000)
    title: str | None = Field(default=None, max_length=300)
