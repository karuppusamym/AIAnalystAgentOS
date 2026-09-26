"""Workspace semantic model (P4-K03, SEM-001..005): the internal shape behind Apache Ossie 0.1.1.

The fields mirror Ossie's (datasets, fields with dimensions, relationships, metrics, `ai_context`,
`custom_extensions`) so a document round-trips without loss. What Ossie has no field for (display
name, format, grain, filters, the dataset a KPI is defined on) travels in one `COMMON` custom extension
whose data is `{"analystos": {...}}`: other tools ignore it, we read it back. Status, version and
approvals are deliberately *not* part of the definition: Ossie has no such fields, and an import can
never carry an approval into a workspace.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MetricStatus = Literal["draft", "proposed", "approved", "deprecated", "rejected"]
ModelStatus = Literal["draft", "proposed", "approved", "deprecated"]
MetricFormat = Literal["number", "percent", "hours", "currency"]  # = MetricDef.format


Cardinality = Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
FilterOp = Literal["=", "!=", "<", "<=", ">", ">=", "in", "not_in", "is_null", "is_not_null"]
TimeGrain = Literal["day", "week", "month", "quarter", "year"]
Scalar = str | int | float | bool
_NAME = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$"  # field, or dataset.field when names collide


class SemanticFilter(BaseModel):
    """One predicate on a model field. Values become literals in the compiler, never SQL text."""

    model_config = ConfigDict(extra="forbid")
    field: str = Field(pattern=_NAME, max_length=241)
    op: FilterOp = "="
    value: Scalar | list[Scalar] | None = None

    @model_validator(mode="after")
    def _value_matches_op(self) -> SemanticFilter:
        if self.op in ("is_null", "is_not_null"):
            if self.value is not None:
                raise ValueError(f"{self.op} takes no value")
        elif self.op in ("in", "not_in"):
            if not isinstance(self.value, list) or not self.value or len(self.value) > 200:
                raise ValueError(f"{self.op} needs a list of 1..200 values")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"{self.op} needs one value")
        return self


class SemanticTime(BaseModel):
    """A time dimension, its grain (a truncation that becomes a group) and an absolute window [start, end)."""

    model_config = ConfigDict(extra="forbid")
    dimension: str = Field(pattern=_NAME, max_length=241)
    grain: TimeGrain | None = None
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def _window(self) -> SemanticTime:
        if self.start and self.end and self.start >= self.end:
            raise ValueError("time.start must be before time.end")
        if self.grain is None and self.start is None and self.end is None:
            raise ValueError("time needs a grain or a window")
        return self


class SemanticOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(pattern=_NAME, max_length=241)  # a requested metric or dimension
    direction: Literal["asc", "desc"] = "asc"


class SemanticQuery(BaseModel):
    """ADR-0019 IR. Names only: metrics, model fields and literal values. SQL comes from the approved
    definitions and the compiler (`semantic/compiler.py`), never from whoever chose the query."""

    model_config = ConfigDict(extra="forbid")
    metrics: list[str] = Field(min_length=1, max_length=20)
    dimensions: list[str] = Field(default_factory=list, max_length=10)
    filters: list[SemanticFilter] = Field(default_factory=list, max_length=20)
    time: SemanticTime | None = None
    order: list[SemanticOrder] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=500, ge=1, le=5000)


class DialectExpression(BaseModel):
    dialect: str = "ANSI_SQL"
    expression: str


class SemanticField(BaseModel):
    """A row-level attribute; `dimension` is kept as Ossie spells it ({is_time}) so None stays None."""

    name: str
    expressions: list[DialectExpression]
    dimension: dict[str, Any] | None = None
    label: str | None = None
    description: str | None = None
    ai_context: str | dict[str, Any] | None = None
    custom_extensions: list[dict[str, Any]] = Field(default_factory=list)


class SemanticDataset(BaseModel):
    name: str
    source: str
    primary_key: list[str] | None = None
    unique_keys: list[list[str]] | None = None
    description: str | None = None
    ai_context: str | dict[str, Any] | None = None
    fields: list[SemanticField] = Field(default_factory=list)
    custom_extensions: list[dict[str, Any]] = Field(default_factory=list)


class SemanticRelationship(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    from_dataset: str = Field(alias="from")
    to: str
    from_columns: list[str]
    to_columns: list[str]
    ai_context: str | dict[str, Any] | None = None
    custom_extensions: list[dict[str, Any]] = Field(default_factory=list)
    # AnalystOS extension (P7-09; Ossie 0.1.1 has no slot): measured through the gateway and accepted by a
    # person in the relationship review queue, never taken from a model. Read from `from` to `to`.
    cardinality: Cardinality | None = None
    validated_at: str | None = None
    validated_by: str | None = None

    @property
    def validated(self) -> bool:
        return bool(self.cardinality and self.validated_at and self.validated_by)


class SemanticMetricDef(BaseModel):
    """One KPI definition. `expressions[0]` is the primary expression (what the platform executes)."""

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=120)
    expressions: list[DialectExpression] = Field(min_length=1)
    description: str | None = None
    ai_context: str | dict[str, Any] | None = None
    custom_extensions: list[dict[str, Any]] = Field(default_factory=list)  # other vendors' extensions, verbatim
    # AnalystOS extension (Ossie 0.1.1 has no field for these)
    display_name: str | None = None
    format: MetricFormat | None = None
    grain: str | None = None
    filters: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    source_columns: list[str] = Field(default_factory=list)
    dataset: str | None = None
    datatype: str | None = None  # Ossie 0.2 / newer metricflow emit it; 0.1.1 has no slot, so it rides in the extension
    # Relationships along which the compiler may pre-aggregate the far side to DISTINCT (join key, field)
    # rows before joining, so an additive measure is not multiplied (ADR-0019 fan-out fix; P7-02).
    pre_aggregations: list[str] = Field(default_factory=list)

    @property
    def expression(self) -> str:
        return self.expressions[0].expression

    @property
    def dialect(self) -> str:
        return self.expressions[0].dialect


class SemanticModelDoc(BaseModel):
    name: str
    description: str | None = None
    ai_context: str | dict[str, Any] | None = None
    datasets: list[SemanticDataset] = Field(default_factory=list)
    relationships: list[SemanticRelationship] = Field(default_factory=list)
    metrics: list[SemanticMetricDef] = Field(default_factory=list)
    custom_extensions: list[dict[str, Any]] = Field(default_factory=list)


class MetricProposalIn(BaseModel):
    """API body for a user proposal."""

    name: str
    expression: str
    dialect: str = "ANSI_SQL"
    display_name: str | None = None
    description: str | None = None
    format: MetricFormat | None = None
    grain: str | None = None
    filters: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    dataset: str | None = None
    ai_context: str | dict[str, Any] | None = None


class SemanticConflict(BaseModel):
    """SEM-005: a duplicate or conflicting KPI, flagged for a person (never silently dropped)."""

    kind: Literal["duplicate_expression", "conflicting_definition", "denominator_mismatch"]
    names: list[str]
    metrics: list[dict[str, Any]]  # [{name, version, status, expression}]
    detail: str
