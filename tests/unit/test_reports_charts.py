"""Static chart PNGs for reports: each chart type renders or cleanly returns None."""
from __future__ import annotations

import pytest
from tests.report_sample import sample_report

from analystos.contracts.reports import ReportChart
from analystos.reports.charts import render_chart_png

PNG = b"\x89PNG\r\n\x1a\n"


def _c(ctype: str, columns: list[str], rows: list[list]) -> ReportChart:
    return ReportChart(key=ctype, title=f"{ctype} chart", chart_type=ctype, columns=columns, rows=rows)


GOOD = [
    _c("line", ["week", "sla"], [[f"W{i}", 0.8 + i / 100] for i in range(12)]),
    _c("line", ["day", "priority", "n"], [[d, p, i + j] for i, d in enumerate(["Mon", "Tue", "Wed"]) for j, p in enumerate(["P1", "P2"])]),
    _c("bar", ["group", "hours"], [["Network", 14.2], ["Service Desk", 6.1], ["Database", 9.8]]),
    _c("bar", ["a very long assignment group name", "n"], [["Global Infrastructure Operations", 3], ["Desk", 5]]),
    _c("stacked_bar", ["week", "priority", "n"], [[w, p, w + len(p)] for w in range(4) for p in ["P1", "P2", "P3"]]),
    _c("histogram", ["bin", "count"], [["0-4", 10], ["4-8", 22], ["8-12", 7]]),
    _c("histogram", ["hours"], [[i % 17 * 1.5] for i in range(200)]),
    _c("scatter", ["reassignments", "hours"], [[i % 7, i * 0.9] for i in range(60)]),
    _c("heatmap", ["dow", "hour", "n"], [[d, h, (i + h) % 5] for i, d in enumerate(["Mon", "Tue"]) for h in range(4)]),
    _c("heatmap", ["group", "P1", "P2"], [["Network", 3, 4], ["Desk", 1, 9]]),
    _c("pie", ["priority", "count"], [["P1", 84], ["P2", 310], ["P3", 640]]),
    _c("pie", ["cat", "n"], [[f"c{i}", i + 1] for i in range(12)]),  # folded into "Other"
    _c("treemap", ["group", "n"], [["Network", 40], ["Desk", 120], ["DB", 30], ["Apps", 60], ["HR", 8]]),
]


@pytest.mark.parametrize("chart", GOOD, ids=lambda c: f"{c.chart_type}-{len(c.columns)}")
def test_renders_png(chart):
    b = render_chart_png(chart)
    assert b is not None and b.startswith(PNG) and len(b) > 1000


@pytest.mark.parametrize("chart", [
    _c("kpi", ["value"], [[0.87]]),
    _c("table", ["a", "b"], [[1, 2]]),
    _c("line", ["week", "sla"], []),  # empty
    _c("bar", [], []),  # no columns
    _c("line", ["week", "label"], [["W1", "abc"], ["W2", "def"]]),  # no numeric measure
    _c("scatter", ["x", "y"], [["a", "b"], [None, None]]),
    _c("pie", ["k", "v"], [["a", -1], ["b", 0]]),  # nothing positive
    _c("histogram", ["bin", "count"], [["0-4"], [None, None, None], [{"x": 1}, "abc"]]),  # ragged/garbage
    _c("heatmap", ["x", "y", "v"], [["a", "b", None]]),
    _c("sankey", ["a", "b"], [[1, 2]]),  # unknown type
    _c("line", ["x", "y"], [[1, float("nan")], [2, float("inf")]]),
], ids=lambda c: f"{c.chart_type}-{len(c.rows)}")
def test_returns_none(chart):
    assert render_chart_png(chart) is None


def test_sample_report_charts_never_raise_and_are_deterministic():
    charts = sample_report().charts
    out = [render_chart_png(c, width_px=600, height_px=300) for c in charts]
    assert [o is not None for o in out] == [True, True, True, False, False]  # empty heatmap + malformed histogram skipped
    assert render_chart_png(charts[0]) == render_chart_png(charts[0])


def test_size_is_clamped():
    b = render_chart_png(GOOD[0], width_px=10, height_px=10_000_000)
    assert b is not None and b.startswith(PNG)
