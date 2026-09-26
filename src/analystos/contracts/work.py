"""Typed work orders (workspace spec §3, spec v4 §4, P4-06).

`WorkOrderSpec` is a routing envelope: objective, job kind, input and semantic versions, budget,
expected outputs and validation policy around exactly one typed execution payload. `AnalysisSpec`
is carried unchanged inside `AnalysisWork`; `PipelineSpec` (P6), `MLSpec` (P5) and `ExperimentSpec`
are typed placeholders their rows fill in. A placeholder validates its envelope fields but is not
executable yet: starting one is refused with `unsupported_capability`, never approximated.

Nothing a client sends here is proof of anything: a `scope_hash` is informational (scope is resolved
server-side at execution), and budgets are clamped to policy by the server.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from analystos.contracts.analysis import AnalysisSpec

JobKind = Literal["describe", "compare", "diagnose", "forecast", "predict", "experiment", "prepare", "monitor"]
WORK_ORDER_SCHEMA_VERSION = 1


class InputRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str | None = None
    source_id: str | None = None
    asset: str | None = None  # schema.table
    version: int | str | None = None


class WorkBudget(BaseModel):
    """Requested limits; the server clamps them to workspace and platform policy."""

    model_config = ConfigDict(extra="forbid")
    max_wall_seconds: int | None = Field(default=None, ge=1, le=86_400)
    max_queries: int | None = Field(default=None, ge=1, le=10_000)
    max_trials: int | None = Field(default=None, ge=1, le=1_000)
    max_cost_usd: float | None = Field(default=None, ge=0, le=1_000)


class ValidationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    require_second_method: bool = False
    confirmatory: bool = False
    min_group_size: int | None = Field(default=None, ge=1)


class AnalysisWork(BaseModel):
    """Describe / compare / diagnose / monitor: a frozen set of `AnalysisSpec`s replayed as given."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["analysis"] = "analysis"
    analyses: list[AnalysisSpec] = Field(min_length=1, max_length=50)
    statements: list[str] = Field(default_factory=list)  # optional plain-language claim per analysis


class PipelineSpec(BaseModel):
    """Placeholder for P6-04 (recipe IR). Envelope-level fields only; not executable yet."""

    model_config = ConfigDict(extra="allow")
    type: Literal["pipeline"] = "pipeline"
    recipe: dict[str, Any] = Field(default_factory=dict)
    output_grain: list[str] = Field(default_factory=list)


class MLSpec(BaseModel):
    """Placeholder for P5-01 (ADR-0024). Envelope-level fields only; not executable yet."""

    model_config = ConfigDict(extra="allow")
    type: Literal["ml"] = "ml"
    task: Literal["forecast", "classify", "regress", "cluster", "anomaly"]
    target_metric: str | None = None
    time_column: str | None = None
    horizon: int | None = Field(default=None, ge=1)
    seed: int | None = None


class ExperimentSpec(BaseModel):
    """Placeholder for P5 experiment design. Not executable yet."""

    model_config = ConfigDict(extra="allow")
    type: Literal["experiment"] = "experiment"
    hypothesis: str | None = None


WorkPayload = Annotated[AnalysisWork | PipelineSpec | MLSpec | ExperimentSpec, Field(discriminator="type")]
EXECUTABLE_TYPES = frozenset({"analysis"})


class WorkOrderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = WORK_ORDER_SCHEMA_VERSION
    kind: JobKind
    objective: str = Field(min_length=10, max_length=4000)
    brief_revision: int | None = None
    inputs: list[InputRef] = Field(default_factory=list)
    semantic_version: str | None = None
    scope_hash: str | None = None  # informational only; never trusted as authorization
    acceptance: list[str] = Field(default_factory=list, max_length=20)
    budget: WorkBudget = Field(default_factory=WorkBudget)
    outputs: list[str] = Field(default_factory=list, max_length=20)
    validation: ValidationPolicy = Field(default_factory=ValidationPolicy)
    source_ids: list[str] | None = None
    spec: WorkPayload

    @property
    def executable(self) -> bool:
        return self.spec.type in EXECUTABLE_TYPES
