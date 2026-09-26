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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


MLTask = Literal["forecast", "classify", "regress", "cluster", "anomaly"]
SplitStrategy = Literal["chronological", "group", "group_chronological", "random"]
# The estimator allowlist per task (ADR-0024 decision 1). The first entry is the task's mandatory baseline;
# widening this list is a capability version change with its own evaluation, not a configuration toggle.
ESTIMATORS: dict[str, tuple[str, ...]] = {
    "classify": ("dummy_prior", "logistic", "gradient_boosting", "random_forest"),
    "regress": ("dummy_mean", "linear", "gradient_boosting", "random_forest"),
    "forecast": ("seasonal_naive", "ets", "arima"),
    "cluster": ("random_partition", "kmeans", "gmm"),
    "anomaly": ("robust_z", "seasonal_residual", "isolation_forest"),
}
# Objective metrics per task; the first is the default. `lower` = smaller is better.
METRICS: dict[str, tuple[str, ...]] = {
    "classify": ("roc_auc", "log_loss", "balanced_accuracy", "f1", "accuracy"),
    "regress": ("mae", "rmse", "r2"),
    "forecast": ("mae", "rmse"),
    "cluster": ("silhouette",),
    "anomaly": ("f1", "recall", "precision"),
}
LOWER_IS_BETTER = frozenset({"log_loss", "mae", "rmse"})


class DatasetRef(BaseModel):
    """The table an ML spec learns from, read through the gateway into an immutable snapshot. `version`
    pins that snapshot (its content hash): data that changed since refuses the experiment."""

    model_config = ConfigDict(extra="forbid")
    asset: str = Field(pattern=r"^[A-Za-z0-9_$-]+\.[A-Za-z0-9_$-]+$")  # schema.table
    source_id: str | None = None
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FeatureSpec(BaseModel):
    """One candidate feature and when its value is known relative to the prediction cutoff."""

    model_config = ConfigDict(extra="forbid")
    column: str = Field(min_length=1, max_length=200)
    type: Literal["numeric", "categorical", "boolean", "datetime"] | None = None  # None: from the catalog / values
    # cutoff: observed at or before the prediction cutoff; known_in_advance: e.g. a calendar attribute;
    # after_outcome: only known once the outcome happened -- always refused (leakage by declaration)
    available_at: Literal["cutoff", "known_in_advance", "after_outcome"] = "cutoff"
    timestamp_column: str | None = None  # when this feature's value was observed; checked against the cutoff


class SplitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: SplitStrategy
    holdout_fraction: float = Field(default=0.2, ge=0.05, le=0.5)
    validation_folds: int = Field(default=3, ge=2, le=10)
    embargo_periods: int = Field(default=0, ge=0, le=365)  # distinct time values dropped before the holdout
    independence_justification: str | None = Field(default=None, max_length=1000)  # required for `random`

    @model_validator(mode="after")
    def _random_needs_a_reason(self) -> SplitSpec:
        if self.strategy == "random" and not (self.independence_justification or "").strip():
            raise ValueError("a random split needs independence_justification (why rows are independent); "
                             "use chronological or group-aware validation otherwise")
        return self


class Preprocessing(BaseModel):
    """Fitted on training folds only and carried unchanged into validation, holdout and scoring."""

    model_config = ConfigDict(extra="forbid")
    numeric_impute: Literal["median", "mean", "zero"] = "median"
    scale: bool = True
    categorical_impute: Literal["most_frequent", "constant"] = "constant"
    max_categories: int = Field(default=30, ge=2, le=500)


class SearchBudget(BaseModel):
    """Requested caps; the platform clamps each to its hard ceiling (settings ml_max_*)."""

    model_config = ConfigDict(extra="forbid")
    max_trials: int = Field(default=12, ge=1, le=200)
    max_seconds: int = Field(default=300, ge=1, le=3600)
    max_rows: int = Field(default=100_000, ge=50, le=5_000_000)


class SliceGuardrail(BaseModel):
    """Within every slice of `column` with at least `min_rows` holdout rows, the candidate may be worse than
    the baseline by at most `max_degradation` (objective metric units); otherwise it is not promotable."""

    model_config = ConfigDict(extra="forbid")
    column: str
    min_rows: int = Field(default=30, ge=5)
    max_degradation: float = Field(default=0.05, ge=0)


class ErrorCosts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    false_positive: float = Field(default=1.0, gt=0)
    false_negative: float = Field(default=1.0, gt=0)


class MLSpec(BaseModel):
    """P5-01 (ADR-0024, workspace spec §6): what a governed model learns from and how it is judged. It pins
    the dataset version, entity/group keys, target, feature availability, prediction cutoff, label
    horizon, split strategy, preprocessing, allowed estimators, search budget, objective metric, slice
    guardrails, seed and (optionally) the runtime environment digest.

    Envelope use stays light (a work order may name only `task`); `executable_problems` lists what a spec
    still lacks before an experiment may start. A model may *propose* a spec; this contract and the
    readiness checks decide."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["ml"] = "ml"
    task: MLTask
    dataset: DatasetRef | None = None
    target: str | None = None  # label (classify/regress; optional 0/1 anomaly labels) or the series value (forecast)
    target_metric: str | None = None  # informational: the semantic metric the target measures
    positive_class: str | int | bool | None = None
    target_unit: str | None = Field(default=None, max_length=40)  # business unit of regression errors
    entity_keys: list[str] = Field(default_factory=list, max_length=5)  # identify a row's entity in outputs
    group_keys: list[str] = Field(default_factory=list, max_length=5)  # repeated entities: never cross partitions
    time_column: str | None = None
    cutoff_column: str | None = None  # per-row prediction time (features must be observed at or before it)
    prediction_cutoff: str | None = None  # ISO datetime: one cutoff for every row
    outcome_time_column: str | None = None  # when the label became known (post-outcome and maturity checks)
    label_horizon: int | None = Field(default=None, ge=1, le=3650)  # days after the cutoff until a label is mature
    horizon: int | None = Field(default=None, ge=1, le=366)  # forecast steps
    season_length: int | None = Field(default=None, ge=2, le=366)
    features: list[FeatureSpec] = Field(default_factory=list, max_length=500)
    split: SplitSpec | None = None
    preprocessing: Preprocessing = Field(default_factory=Preprocessing)
    estimators: list[str] = Field(default_factory=list)  # empty: the task's whole allowlist
    search: SearchBudget = Field(default_factory=SearchBudget)
    objective_metric: str | None = None
    min_improvement: float = Field(default=0.0, ge=0)  # over the baseline, in objective units
    error_costs: ErrorCosts = Field(default_factory=ErrorCosts)  # classification threshold choice
    guardrails: list[SliceGuardrail] = Field(default_factory=list, max_length=10)
    k_range: tuple[int, int] = (2, 8)  # cluster
    contamination: float = Field(default=0.02, gt=0, lt=0.5)  # anomaly: expected share of anomalies
    seed: int | None = None
    runtime_digest: str | None = None  # when set, training refuses another environment

    @model_validator(mode="after")
    def _allowlists(self) -> MLSpec:
        allowed = ESTIMATORS[self.task]
        bad = [e for e in self.estimators if e not in allowed]
        if bad:
            raise ValueError(f"estimators {bad} are not allowlisted for {self.task}; allowed: {list(allowed)}")
        if self.objective_metric is not None and self.objective_metric not in METRICS[self.task]:
            raise ValueError(f"objective_metric {self.objective_metric!r} is not a {self.task} metric; "
                             f"one of {list(METRICS[self.task])}")
        lo, hi = self.k_range
        if not 2 <= lo <= hi <= 20:
            raise ValueError("k_range must satisfy 2 <= low <= high <= 20")
        names = [f.column for f in self.features]
        if len(names) != len(set(names)):
            raise ValueError("a feature is listed twice")
        return self

    @property
    def metric(self) -> str:
        return self.objective_metric or METRICS[self.task][0]

    @property
    def candidates(self) -> list[str]:
        """Allowlisted estimators to search, baseline excluded (it always runs)."""
        chosen = self.estimators or list(ESTIMATORS[self.task])
        return [e for e in chosen if e != ESTIMATORS[self.task][0]]

    @property
    def baseline(self) -> str:
        return ESTIMATORS[self.task][0]

    def executable_problems(self) -> list[str]:
        """What must be added before an experiment can start (empty = executable)."""
        out = []
        if self.dataset is None:
            out.append("dataset (the table to learn from) is required")
        if self.task in ("classify", "regress") and not self.target:
            out.append(f"{self.task} needs a target column")
        if self.task == "forecast":
            if not self.target:
                out.append("forecast needs target (the series value column)")
            if not self.time_column:
                out.append("forecast needs time_column")
            if not self.horizon:
                out.append("forecast needs horizon")
        if self.task in ("classify", "regress", "cluster", "anomaly") and not self.features:
            out.append(f"{self.task} needs at least one feature" + (" (the monitored values)" if self.task == "anomaly" else ""))
        if not self.candidates:
            out.append("no candidate estimator besides the baseline")
        return out


class MLScoringSpec(BaseModel):
    """A batch-scoring definition (kind `ml_scoring`, P5-03). Published and pinned: it names the exact model
    version and package hash, the input table and the managed output it writes. Scoring a new version is a
    new definition version, never a silent switch."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=120)  # the model name (its ml_spec definition key)
    model_version_id: str = Field(pattern=r"^mlv_[0-9a-f]+$")
    package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: DatasetRef | None = None  # None only for a forecast (it writes its stored forward forecast)
    output: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")  # output table name in the managed output source


class ExperimentSpec(BaseModel):
    """One bounded experiment over a *published* `ml_spec` definition (P5-01/P5-02): which version, an
    optional hypothesis, and budget overrides that can only tighten the definition's own."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["experiment"] = "experiment"
    hypothesis: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any] | None = None  # {key, version} of a published ml_spec (kind ml_spec)
    max_trials: int | None = Field(default=None, ge=1, le=200)
    max_seconds: int | None = Field(default=None, ge=1, le=3600)


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
