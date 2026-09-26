"""Report contract (RPT-001..003). A report is rendered from this immutable snapshot, so the same
ReportData always yields the same document and its hash can be approved before delivery."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ReportQuery(BaseModel):
    id: str
    sql: str
    row_count: int = 0
    result_hash: str | None = None


class ReportInsight(BaseModel):
    code: str
    title: str
    finding: str
    confidence: float
    verified: bool
    caveats: list[str] = Field(default_factory=list)
    business_impact: dict[str, Any] = Field(default_factory=dict)
    evidence_queries: list[ReportQuery] = Field(default_factory=list)
    change: Literal["new", "persisting", "changed", None] = None  # vs previous run of the same schedule
    # Typed evidence (P4-03): validation state (exploratory | confirmed | legacy | ...) and staleness. When
    # set, reports label the finding discovery/confirmed and call the confidence an uncalibrated review score.
    validation: str | None = None
    stale: bool = False

    def evidence_label(self) -> str | None:
        if self.validation is None:
            return None
        label = {"confirmed": "confirmed", "exploratory": "discovery (exploratory)", "replicated": "discovery (replicated)",
                 "legacy": "legacy verification"}.get(self.validation, self.validation.replace("_", " "))
        return label + ("; stale: data changed, needs re-verification" if self.stale else "")


class ReportMetric(BaseModel):
    name: str
    display_name: str
    definition: str = ""
    value: float | int | str | None = None
    previous_value: float | int | str | None = None
    format: Literal["number", "percent", "hours", "currency"] = "number"


class ReportChart(BaseModel):
    key: str
    title: str
    chart_type: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)


class ReportAlert(BaseModel):
    severity: Literal["info", "warning", "critical"]
    title: str
    message: str
    metric: str | None = None


class ReportData(BaseModel):
    kind: Literal["executive", "operational", "statistical", "exception", "weekly_summary"] = "executive"
    title: str
    workspace_name: str
    objective: str
    run_id: str
    generated_at: str  # ISO timestamp
    period: str | None = None  # e.g. "2026-09-15 .. 2026-09-21"
    summary_markdown: str = ""
    insights: list[ReportInsight] = Field(default_factory=list)
    resolved_insights: list[ReportInsight] = Field(default_factory=list)  # present last time, gone now
    metrics: list[ReportMetric] = Field(default_factory=list)
    charts: list[ReportChart] = Field(default_factory=list)
    quality_issues: list[dict[str, Any]] = Field(default_factory=list)  # {severity, asset, column, message}
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)  # {code, statement, status, conclusion}
    alerts: list[ReportAlert] = Field(default_factory=list)
    lineage_note: str = ""
    caveats: list[str] = Field(default_factory=list)
