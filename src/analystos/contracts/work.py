"""Typed work orders (workspace spec §3, spec v4 §4, P4-06).

`WorkOrderSpec` is a routing envelope: objective, job kind, input and semantic versions, budget,
expected outputs and validation policy around exactly one typed execution payload. `AnalysisSpec`
is carried unchanged inside `AnalysisWork`; `PipelineSpec` (P6-01) is the typed envelope around published
recipes; `MLSpec` (P5) and `ExperimentSpec` are typed placeholders their rows fill in. Only `analysis`
starts as an analysis run: any other payload validates and persists, and starting it as a run is refused
with `unsupported_capability`, never approximated (a PipelineSpec runs through the pipeline API).

Nothing a client sends here is proof of anything: a `scope_hash` is informational (scope is resolved
server-side at execution), and budgets are clamped to policy by the server.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from analystos.contracts.analysis import AnalysisSpec
from analystos.contracts.recipe import Cardinality, Column, Incremental

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


class RecipeRef(BaseModel):
    """A recipe step of a pipeline: its name, and a version to pin (None = the published version at run time)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,55}$")
    version: int | None = Field(default=None, ge=1)


class PipelineInput(BaseModel):
    """An input version: the asset, optionally its pinned content fingerprint (a changed input refuses the
    run) and how old its last staging may be."""

    model_config = ConfigDict(extra="forbid")
    asset: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    fingerprint: str | None = None
    max_age_hours: float | None = Field(default=None, gt=0)


class PipelineOutput(BaseModel):
    """The output contract: grain, keys and schema must equal the recipe output's, so a recipe change that
    alters them fails the pipeline instead of silently changing what downstream reads."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    recipe: str | None = None  # the step producing it (default: the last recipe)
    output: str  # the recipe's output name
    grain: list[str] = Field(default_factory=list)
    keys: list[str] = Field(default_factory=list)
    output_schema: list[Column] = Field(alias="schema", min_length=1)


class JoinExpectation(BaseModel):
    """What a join may do to the data: the declared cardinality (equal to the recipe's), the share of left
    rows allowed to find no match, and the row multiplication allowed (1.0 = none)."""

    model_config = ConfigDict(extra="forbid")
    node: str
    expected_cardinality: Cardinality
    max_unmatched_pct: float | None = Field(default=None, ge=0, le=100)
    max_row_multiplication: float | None = Field(default=None, ge=1)


class ReconciliationCheck(BaseModel):
    """Aggregate reconciliation: `func(input_column)` over a recipe node against `func(output_column)` over
    the output, within `tolerance_pct` (a business measure must not multiply after a join)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,55}$")
    func: Literal["sum", "count"] = "sum"
    input_node: str
    input_column: str | None = None
    output_column: str | None = None
    tolerance_pct: float = Field(default=0.0, ge=0, le=100)


class PipelineFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_age_hours: float = Field(gt=0, le=24 * 366)  # an alert is raised when the destination's good version is older


class PipelineBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_queries: int | None = Field(default=None, ge=1, le=1_000)
    max_rows: int | None = Field(default=None, ge=1, le=5_000_000)
    max_wall_seconds: int | None = Field(default=None, ge=1, le=86_400)


class PipelineDestination(BaseModel):
    """Where the managed writer materializes the output: an allowlisted schema.table (P6-03)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    engine: Literal["postgres:analytics"] = "postgres:analytics"
    schema_name: str = Field(alias="schema", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    table: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,50}$")


class PipelineSpec(BaseModel):
    """The envelope around one or more published recipes (P6-01, workspace spec §5): input versions, the
    output grain, schema and keys, expected join cardinality, freshness, reconciliation checks, budgets,
    the incremental block and an optional managed destination. Validated against the recipes it names
    (`pipelines/spec.py`); the recipe IR validator and compiler decide what runs."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["pipeline"] = "pipeline"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,55}$")
    description: str = ""
    recipes: list[RecipeRef] = Field(min_length=1, max_length=10)
    inputs: list[PipelineInput] = Field(default_factory=list, max_length=50)
    output: PipelineOutput
    joins: list[JoinExpectation] = Field(default_factory=list, max_length=20)
    checks: list[ReconciliationCheck] = Field(default_factory=list, max_length=20)
    freshness: PipelineFreshness | None = None
    budgets: PipelineBudgets = Field(default_factory=PipelineBudgets)
    incremental: Incremental | None = None
    destination: PipelineDestination | None = None

    def output_recipe(self) -> str:
        return self.output.recipe or self.recipes[-1].name

    def spec(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


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
