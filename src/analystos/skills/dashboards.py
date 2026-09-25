"""Dashboard layout on a 12-column grid (spec §33) and native filter selection.

Layout cells are dicts: {"kind": "chart"|"filters"|"markdown", "chart": key|None, "row", "col", "width", "height"}
(`DashboardSpec.layout` entries; `kind` lets BI adapters render non-chart cells).

executive:  KPI row (up to 4 cards per row, width 3) -> trend(s) full width -> drivers side by side (width 6)
            -> top risk full width -> remaining charts -> executive summary text full width
operational: filter bar full width -> heatmaps / aging / queue side by side (width 6) -> other charts
            side by side -> detail tables full width at the bottom
"""
from __future__ import annotations

from typing import Any

from analystos.contracts.bi import ChartSpec

GRID = 12
H_KPI, H_CHART, H_TABLE, H_TEXT, H_FILTERS = 2, 4, 6, 2, 1
RISK_WORDS = ("risk", "anomal", "exception", "breach", "sla", "overdue", "backlog")
QUEUE_WORDS = ("aging", "queue", "backlog", "open", "sla")
MAX_NATIVE_FILTERS = 6


def _is_risk(c: ChartSpec) -> bool:
    text = f"{c.key} {c.title} {c.intent} {' '.join(c.insight_codes)}".lower()
    return c.intent in ("risk", "exception") or any(w in text for w in RISK_WORDS)


class _Grid:
    def __init__(self) -> None:
        self.cells: list[dict[str, Any]] = []
        self.row = 0

    def full(self, kind: str, chart: str | None, height: int) -> None:
        self.cells.append({"kind": kind, "chart": chart, "row": self.row, "col": 0, "width": GRID, "height": height})
        self.row += height

    def tiles(self, charts: list[str], width: int, height: int, kind: str = "chart") -> None:
        per_row = GRID // width
        for i in range(0, len(charts), per_row):
            chunk = charts[i:i + per_row]
            w = width if len(chunk) == per_row else max(width, GRID // len(chunk))
            for j, key in enumerate(chunk):
                self.cells.append({"kind": kind, "chart": key, "row": self.row, "col": j * w, "width": w, "height": height})
            self.row += height


def build_layout(audience: str, charts: list[ChartSpec], *, include_summary: bool = True) -> list[dict[str, Any]]:
    g = _Grid()
    placed: set[str] = set()

    def take(pred) -> list[ChartSpec]:
        out = [c for c in charts if c.key not in placed and pred(c)]
        placed.update(c.key for c in out)
        return out

    if audience == "executive":
        kpis = take(lambda c: c.chart_type == "kpi")
        if kpis:
            g.tiles([c.key for c in kpis], 3, H_KPI)
        for c in take(lambda c: c.chart_type == "line" or c.intent == "trend"):
            g.full("chart", c.key, H_CHART)
        risks = take(lambda c: _is_risk(c) and c.chart_type != "table")
        drivers = take(lambda c: c.intent in ("comparison", "driver", "relationship", "part_to_whole", "distribution")
                       and c.chart_type != "table")
        if drivers:
            g.tiles([c.key for c in drivers], 6, H_CHART)
        for c in risks[:1]:
            g.full("chart", c.key, H_CHART)
        rest = risks[1:] + take(lambda c: c.chart_type != "table")
        if rest:
            g.tiles([c.key for c in rest], 6, H_CHART)
        for c in take(lambda c: True):
            g.full("chart", c.key, H_TABLE)
        if include_summary:
            g.full("markdown", None, H_TEXT)
        return g.cells
    if audience == "operational":
        g.full("filters", None, H_FILTERS)
        kpis = take(lambda c: c.chart_type == "kpi")
        if kpis:
            g.tiles([c.key for c in kpis], 3, H_KPI)
        ops = take(lambda c: c.chart_type == "heatmap" or c.intent in ("aging", "queue", "cohort")
                   or (any(w in f"{c.key} {c.title}".lower() for w in QUEUE_WORDS) and c.chart_type != "table"))
        if ops:
            g.tiles([c.key for c in ops], 6, H_CHART)
        rest = take(lambda c: c.chart_type != "table")
        if rest:
            g.tiles([c.key for c in rest], 6, H_CHART)
        for c in take(lambda c: True):
            g.full("chart", c.key, H_TABLE)
        return g.cells
    raise ValueError(f"unknown audience {audience!r}; expected executive or operational")


def choose_native_filters(charts: list[ChartSpec], dataset_columns: list[dict[str, Any]], *,
                          max_filters: int = MAX_NATIVE_FILTERS) -> list[str]:
    """Native (dashboard-level) filters: the time column first, then dimensions used by charts, then
    other low-cardinality categorical/boolean columns. Ids, free text and high-cardinality columns
    (> 50 values) are never filters."""
    meta = {c["name"]: c for c in dataset_columns}

    def ok(name: str) -> bool:
        m = meta.get(name)
        if m is None:
            return False
        st = (m.get("semantic_type") or "").lower()
        card = m.get("cardinality", m.get("distinct"))
        if st in ("id", "text", "numeric"):
            return False
        return not (card is not None and st != "datetime" and card > 50)

    out: list[str] = []
    times = [c["name"] for c in dataset_columns if (c.get("semantic_type") or "").lower() == "datetime"]
    if times:
        out.append(times[0])
    used = [x for c in charts for x in (c.dimension, c.series) if x]
    for name in used:
        if name not in out and ok(name):
            out.append(name)
    extra = sorted((c for c in dataset_columns if (c.get("semantic_type") or "").lower() in ("categorical", "boolean")
                    and c["name"] not in out and ok(c["name"])),
                   key=lambda c: (c.get("cardinality", c.get("distinct")) or 0, c["name"]))
    out += [c["name"] for c in extra]
    return out[:max_filters]
