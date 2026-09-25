"""Workspace semantic model (P4-K03, SEM-001..005): the internal shape behind Apache Ossie 0.1.1.

The fields mirror Ossie's (datasets, fields with dimensions, relationships, metrics, `ai_context`,
`custom_extensions`) so a document round-trips without loss. What Ossie has no field for (display
name, format, grain, filters, the dataset a KPI is defined on) travels in one `COMMON` custom extension
whose data is `{"analystos": {...}}`: other tools ignore it, we read it back. Status, version and
approvals are deliberately *not* part of the definition: Ossie has no such fields, and an import can
never carry an approval into a workspace.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MetricStatus = Literal["draft", "proposed", "approved", "deprecated", "rejected"]
ModelStatus = Literal["draft", "proposed", "approved", "deprecated"]
MetricFormat = Literal["number", "percent", "hours", "currency"]  # = MetricDef.format


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

    kind: Literal["duplicate_expression", "conflicting_definition"]
    names: list[str]
    metrics: list[dict[str, Any]]  # [{name, version, status, expression}]
    detail: str
