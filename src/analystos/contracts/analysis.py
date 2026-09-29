"""Executable analysis contracts: hypotheses compile to SQL from a closed vocabulary, not free text.

Why a closed vocabulary: the LLM proposes *what* to test; deterministic code decides *how* the
number is computed. That keeps every finding reproducible and keeps the model away from raw SQL
on the critical path (the SQL agent's free-form SQL still exists for ad-hoc questions and goes
through the same gateway validation).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _method_schema(schema: dict[str, Any]) -> None:
    from analystos import methods

    methods.schema_extra(schema)


class Derivation(BaseModel):
    """A column or a safe derived expression over one column (or two for durations).

    type:
      column          -> the raw column
      duration_hours  -> (end - start) in hours; `column` is start, `end_column` is end
      after_hours     -> TRUE when hour(column) outside [start_hour, end_hour) or weekend
      bucket          -> numeric column bucketed by `edges` (labels like "0", "1", "2", "3+")
      equals          -> TRUE when column = value (boolean outcome from a categorical)
      is_true         -> column interpreted as boolean
      date_trunc      -> date_trunc(grain, column)
      hour_of_day     -> extract(hour from column)
      day_of_week     -> extract(dow from column)
    """

    type: Literal["column", "duration_hours", "after_hours", "bucket", "equals", "is_true",
                  "date_trunc", "hour_of_day", "day_of_week"] = "column"
    column: str
    end_column: str | None = None
    value: Any = None
    edges: list[float] | None = None
    grain: Literal["day", "week", "month", "quarter"] | None = None
    start_hour: int = 8
    end_hour: int = 18
    label: str | None = None

    def columns(self) -> list[str]:
        return [c for c in (self.column, self.end_column) if c]


class Filter(BaseModel):
    column: str
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in", "is null", "is not null"]
    value: Any = None
    origin: Literal["plan", "user_redirect", "policy"] = "plan"


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
    min_group_size: int = 30
    top_k: int = 12

    @field_validator("method")
    @classmethod
    def _registered(cls, v: str) -> str:
        from analystos import methods

        if v not in methods.names():
            raise ValueError(f"unknown analysis method {v!r}; registered methods: {', '.join(methods.names())}")
        return v


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


class Partition(BaseModel):
    """A deterministic split of one table's rows into a discovery part and a held-out part (P8-15).

    A row's bucket is the first 16 bits of md5(salt | key columns as text), 0..65535; rows whose bucket is
    below ``fraction * 65536`` are held out. The same rows and key give the same split on every run.
    `side` is the part a query reads. The held-out side compiles only with `claim_locked_at` set: the claim
    it confirms was locked first (`evidence.holdout`). Kept out of `AnalysisSpec` on purpose, so no
    proposal (model or rule) can ask to read the held-out rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    side: Literal["discovery", "holdout"] = "discovery"
    key: list[str] = Field(min_length=1)  # the columns hashed: the table's key, or every readable column
    basis: str = "full_row"  # approved_key | declared_key | measured_unique_key | profile_unique_key | full_row
    fraction: float = Field(gt=0, lt=1)  # share of rows held out
    salt: str = "analystos.holdout.v1"
    claim_locked_at: str | None = None
