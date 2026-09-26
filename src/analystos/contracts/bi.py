"""Visualization + dashboard contracts consumed by BI adapters (§32-§34)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer, model_validator

ChartType = Literal["kpi", "line", "bar", "stacked_bar", "histogram", "scatter", "heatmap", "table", "pie", "treemap"]


class MetricDef(BaseModel):
    """Semantic metric (§24). `sql_expression` is an aggregate over the dataset columns."""

    name: str
    display_name: str
    definition: str
    sql_expression: str  # e.g. "AVG(resolution_hours)"
    format: Literal["number", "percent", "hours", "currency"] = "number"
    grain: str = ""
    filters: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    owner: str | None = None
    source_columns: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    status: Literal["proposed", "validated", "approved"] = "proposed"


class DatasetDef(BaseModel):
    """A reusable analytical dataset: a governed SELECT (virtual dataset; no source writes in MVP)."""

    name: str
    description: str = ""
    sql: str
    columns: list[dict[str, Any]] = Field(default_factory=list)  # {name, type, semantic_type, business_name}
    time_column: str | None = None
    source_assets: list[str] = Field(default_factory=list)
    row_count: int | None = None


class ChartSpec(BaseModel):
    key: str
    title: str
    chart_type: ChartType
    intent: str  # trend | comparison | distribution | relationship | kpi | detail | part_to_whole
    dataset: str  # DatasetDef.name
    metric: str | None = None  # MetricDef.name
    metrics: list[str] = Field(default_factory=list)
    dimension: str | None = None
    series: str | None = None
    time_grain: Literal["day", "week", "month"] | None = None
    filters: list[str] = Field(default_factory=list)
    limit: int | None = None
    description: str = ""
    insight_codes: list[str] = Field(default_factory=list)
    rationale: str = ""
    preview: dict[str, Any] = Field(default_factory=dict)  # {columns, rows} computed via gateway for UI preview
    # ADR-0019: `governed` only when compiled from a SemanticQuery against an approved model version.
    governance: Literal["governed", "ad_hoc"] = "ad_hoc"
    semantic_model_version: int | None = None
    compiler_version: str | None = None

    @model_validator(mode="after")
    def _governed_carries_versions(self) -> ChartSpec:
        if self.governance == "governed" and (self.semantic_model_version is None or not self.compiler_version):
            raise ValueError("a governed chart carries semantic_model_version and compiler_version")
        return self

    @model_serializer(mode="wrap")
    def _omit_default_label(self, handler: Any) -> dict[str, Any]:
        """An ad-hoc chart serializes as before the label existed, so bundle hashes (and the publish
        approvals bound to them) do not change; the attribute still reads `ad_hoc`."""
        data = handler(self)
        if self.governance == "ad_hoc" and self.semantic_model_version is None and self.compiler_version is None:
            for key in ("governance", "semantic_model_version", "compiler_version"):
                data.pop(key, None)
        return data


class DashboardSpec(BaseModel):
    key: str
    title: str
    audience: Literal["executive", "operational"]
    description: str = ""
    charts: list[str]  # ChartSpec keys, in layout order
    layout: list[dict[str, Any]] = Field(default_factory=list)  # [{chart, row, col, width, height}] 12-col grid
    native_filters: list[str] = Field(default_factory=list)  # dimension columns
    summary_markdown: str = ""


class PublishBundle(BaseModel):
    """Exactly what gets published. Its hash is what an approval binds to."""

    workspace_id: str
    destination: Literal["superset", "powerbi", "preview"] = "superset"
    datasets: list[DatasetDef]
    metrics: list[MetricDef]
    charts: list[ChartSpec]
    dashboards: list[DashboardSpec]


class PublishResult(BaseModel):
    destination: str
    status: Literal["succeeded", "partial", "failed"]
    external_ids: dict[str, Any] = Field(default_factory=dict)  # {"datasets": {name: id}, "charts": {...}, "dashboards": {...}}
    urls: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
