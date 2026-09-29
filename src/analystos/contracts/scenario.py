"""What-if scenarios (N-9): a governed `SemanticQuery` plus declared changes, applied deterministically.

The observed baseline is the compiled query's result through `QueryGateway` (spec v4 P18, ADR-0019).
The scenario is arithmetic in `skills/scenario.py` on that result, or the same query re-measured with
a changed filter value (a threshold). Every number the scenario returns carries its basis: `observed`
(what the data says) or `simulated` (what it would be under the stated assumptions). A simulated
number is never publishable: the result says so, `ChartSpec.value_basis` refuses it in a
`PublishBundle`, and the scenario numbers guard refuses a sentence that quotes one unlabelled.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analystos.contracts.semantic import FilterOp, Scalar, SemanticQuery

SCENARIO_VERSION = "whatif.v1"
ValueBasis = Literal["observed", "simulated"]
_NAME = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$"


class ScenarioSegment(BaseModel):
    """Restrict a change to result rows whose requested dimension holds one of `values`."""

    model_config = ConfigDict(extra="forbid")
    dimension: str = Field(pattern=_NAME, max_length=241)
    values: list[Scalar] = Field(min_length=1, max_length=50)


class ScenarioAdjustment(BaseModel):
    """One change to a requested metric: `scale` by `percent`, `shift` by `amount`, or `set` to `amount`."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["scale", "shift", "set"]
    metric: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=120)
    percent: float | None = Field(default=None, ge=-100, le=1000)
    amount: float | None = Field(default=None, allow_inf_nan=False)
    segment: ScenarioSegment | None = None

    @model_validator(mode="after")
    def _operand(self) -> ScenarioAdjustment:
        if self.kind == "scale" and (self.percent is None or self.amount is not None):
            raise ValueError("scale takes percent (and no amount)")
        if self.kind in ("shift", "set") and (self.amount is None or self.percent is not None):
            raise ValueError(f"{self.kind} takes amount (and no percent)")
        return self


class FilterOverride(BaseModel):
    """A threshold change: the query's filter on `field` takes `value` (and `op`, if given). The query is
    re-measured through the gateway; its numbers answer a changed definition, so they are simulated."""

    model_config = ConfigDict(extra="forbid")
    field: str = Field(pattern=_NAME, max_length=241)
    value: Scalar | list[Scalar] | None = None
    op: FilterOp | None = None


class ScenarioSpec(BaseModel):
    """What the person asks: the governed query, the changes, and the assumptions in their own words."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    semantic_query: SemanticQuery
    adjustments: list[ScenarioAdjustment] = Field(default_factory=list, max_length=10)
    filter_overrides: list[FilterOverride] = Field(default_factory=list, max_length=5)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    ask_turn_id: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _changes(self) -> ScenarioSpec:
        if not self.adjustments and not self.filter_overrides:
            raise ValueError("a scenario needs at least one adjustment or filter override")
        if any(not a.strip() or len(a) > 500 for a in self.assumptions):
            raise ValueError("each assumption is 1..500 characters")
        return self


class LabelledValue(BaseModel):
    """A number and the one thing a reader must know about it: observed or simulated."""

    model_config = ConfigDict(extra="forbid")
    value: float | None
    basis: ValueBasis


class ScenarioCell(BaseModel):
    metric: str
    observed: LabelledValue
    simulated: LabelledValue
    change: LabelledValue
    change_pct: LabelledValue  # a fraction (0.1 = +10%); None when the observed value is 0 or missing
    adjusted_by: list[int] = Field(default_factory=list)  # indexes into spec.adjustments

    @model_validator(mode="after")
    def _labels(self) -> ScenarioCell:
        if self.observed.basis != "observed":
            raise ValueError("the observed value must be labelled observed")
        if any(v.basis != "simulated" for v in (self.simulated, self.change, self.change_pct)):
            raise ValueError("scenario values and their changes must be labelled simulated")
        return self


class ScenarioRow(BaseModel):
    key: dict[str, Any]  # requested dimension (and time grain) -> member
    cells: list[ScenarioCell]


class ScenarioResult(BaseModel):
    """The recorded scenario. `publishable` is always False: a simulated number is not a published number."""

    id: str
    workspace_id: str
    name: str
    scenario_version: str = SCENARIO_VERSION
    label: Literal["simulated"] = "simulated"
    publishable: Literal[False] = False
    spec: dict[str, Any]
    spec_hash: str
    assumptions: list[str]  # the person's, then one generated statement per change
    assumptions_hash: str
    baseline: dict[str, Any]  # {basis: observed, query_id, sql_hash, result_hash, row_count, semantic provenance}
    remeasured: dict[str, Any] | None = None  # the threshold-changed query's receipt, basis simulated
    columns: list[str]
    rows: list[ScenarioRow]
    totals: list[ScenarioCell] = Field(default_factory=list)  # summable metrics only (SUM / COUNT)
    summary: str
    guard: dict[str, Any]
    result_hash: str
    ask_turn_id: str | None = None
    created_by: str
    created_at: datetime | None = None
