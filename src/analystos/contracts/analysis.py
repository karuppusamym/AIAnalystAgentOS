"""Executable analysis contracts: hypotheses compile to SQL from a closed vocabulary, not free text.

Why a closed vocabulary: the LLM proposes *what* to test; deterministic code decides *how* the
number is computed. That keeps every finding reproducible and keeps the model away from raw SQL
on the critical path (the SQL agent's free-form SQL still exists for ad-hoc questions and goes
through the same gateway validation).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator


def _method_schema(schema: dict[str, Any]) -> None:
    from analystos import methods

    methods.schema_extra(schema)


def _omit(data: dict[str, Any], **defaults: Any) -> dict[str, Any]:
    """Drop fields added after specs were first hashed while they hold their default: a spec written
    before them serializes (and hashes) exactly as it did, so registries and pins keep matching."""
    for key, default in defaults.items():
        if key in data and data[key] == default:
            data.pop(key)
    return data


class Derivation(BaseModel):
    """A column or a safe derived expression over one column (or two for durations).

    type:
      column          -> the raw column
      duration_hours  -> (end - start) in hours; `column` is start, `end_column` is end
      later_than      -> TRUE when `end_column` is later than `column` by more than `tolerance_hours`
                         (delivered after promised, resolved after due); NULL when either is NULL
      after_hours     -> TRUE when hour(column) outside [start_hour, end_hour) or weekend
      bucket          -> numeric column bucketed by `edges` (labels like "0", "1", "2", "3+")
      equals          -> TRUE when column = value (boolean outcome from a categorical)
      is_true         -> column interpreted as boolean
      date_trunc      -> date_trunc(grain, column)
      hour_of_day     -> extract(hour from column)
      day_of_week     -> extract(dow from column)

    via: the column(s) live in a related table, reached through the spec's join whose `from_column`
    is `via` (customer region through `customer_id`); None reads the spec's own asset.
    """

    type: Literal["column", "duration_hours", "later_than", "after_hours", "bucket", "equals", "is_true",
                  "date_trunc", "hour_of_day", "day_of_week"] = "column"
    column: str
    end_column: str | None = None
    value: Any = None
    edges: list[float] | None = None
    grain: Literal["day", "week", "month", "quarter"] | None = None
    start_hour: int = 8
    end_hour: int = 18
    tolerance_hours: float = Field(0.0, ge=0)
    via: str | None = None
    label: str | None = None

    def columns(self) -> list[str]:
        return [c for c in (self.column, self.end_column) if c]

    @model_serializer(mode="wrap")
    def _compat(self, handler: Any):  # unannotated: the serialization schema stays the model's
        return _omit(handler(self), tolerance_hours=0.0, via=None)


class Filter(BaseModel):
    column: str
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in", "is null", "is not null"]
    value: Any = None
    origin: Literal["plan", "user_redirect", "policy"] = "plan"
    via: str | None = None  # as Derivation.via: a related table's column

    @model_serializer(mode="wrap")
    def _compat(self, handler: Any):  # unannotated: the serialization schema stays the model's
        return _omit(handler(self), via=None)


class Join(BaseModel):
    """A many-to-one lookup from the spec's asset: `from_column` references the unique `to_column` of
    `asset`. Compiled as a LEFT JOIN, so every base row is kept exactly once; validation accepts only a
    validated (or user-declared) relationship between in-scope tables of one source. Derivations and
    filters name it by `via` = `from_column`."""

    from_column: str
    asset: str  # "schema.table"
    to_column: str


class AnalysisSpec(BaseModel):
    """One executable hypothesis test. `method` must be a registered analysis method: the vocabulary,
    its JSON Schema enum and the per-method requirements come from the method registry
    (`analystos.methods`, spec v3 §3.5), never from a list kept here."""

    method: str = Field(json_schema_extra=_method_schema)
    asset: str  # "schema.table"
    outcome: Derivation | None = None
    segment: Derivation | None = None
    drivers: list[Derivation] = Field(default_factory=list)
    time: Derivation | None = None
    filters: list[Filter] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    min_group_size: int = 30
    top_k: int = 12

    @field_validator("method")
    @classmethod
    def _registered(cls, v: str) -> str:
        from analystos import methods

        if v not in methods.names():
            raise ValueError(f"unknown analysis method {v!r}; registered methods: {', '.join(methods.names())}")
        return v

    @model_validator(mode="after")
    def _vias_declared(self) -> AnalysisSpec:
        keys = [j.from_column for j in self.joins]
        if len(keys) != len(set(keys)):
            raise ValueError("each join needs its own from_column")
        for via in self.vias():
            if via not in keys:
                raise ValueError(f"via {via!r} names no join of the spec (declare it in `joins`)")
        return self

    def derivations(self) -> list[Derivation]:
        return [d for d in (self.outcome, self.segment, self.time, *self.drivers) if d is not None]

    def vias(self) -> list[str]:
        return list(dict.fromkeys(x.via for x in (*self.derivations(), *self.filters) if x.via))

    def join(self, via: str | None) -> Join | None:
        return next((j for j in self.joins if j.from_column == via), None) if via else None

    def table_of(self, item: Derivation | Filter) -> str:
        """The table a derivation's or a filter's columns are read from."""
        j = self.join(item.via)
        return j.asset if j is not None else self.asset

    @model_serializer(mode="wrap")
    def _compat(self, handler: Any):  # unannotated: the serialization schema stays the model's
        return _omit(handler(self), joins=[])


class HypothesisProposal(BaseModel):
    question: str
    statement: str
    rationale: str = ""
    spec: AnalysisSpec
    priority: Literal["high", "medium", "low"] = "medium"


class StatResult(BaseModel):
    """Output of a statistical skill. Every number an insight quotes must come from here."""

    method: str
    test: str
    n: int
    statistic: float | None = None
    p_value: float | None = None
    effect_size: float | None = None
    effect_label: str | None = None  # e.g. "cramers_v", "rate_ratio", "spearman_rho"
    ci_low: float | None = None
    ci_high: float | None = None
    groups: list[dict[str, Any]] = Field(default_factory=list)  # per-segment rows
    highlights: dict[str, Any] = Field(default_factory=dict)  # best/worst segment, ratios, shares
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    supported: bool | None = None  # method-level verdict before REV
    details: dict[str, Any] = Field(default_factory=dict)
