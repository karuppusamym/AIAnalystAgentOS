"""XLSX rendering of ReportData with openpyxl.

Data text is untrusted: every string cell is written with data_type 's', so a value starting with
'=', '+', '-' or '@' is stored as text, never as a formula (CSV/formula injection). Control
characters openpyxl rejects are stripped. Workbook created/modified timestamps come from
ReportData.generated_at; cell content is fully deterministic for a given ReportData.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.chart import BarChart, LineChart, PieChart, Reference, ScatterChart, Series
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from analystos.contracts.reports import ReportChart, ReportData
from analystos.reports import _common as C

NUMBER_FORMATS = {"percent": "0.0%", "number": "#,##0.00", "hours": '#,##0.0" h"', "currency": '"$"#,##0.00'}
CHANGE_FORMATS = {"percent": '+0.0%;-0.0%;0.0%', "number": "+#,##0.00;-#,##0.00;0.00",
                  "hours": '+#,##0.0" h";-#,##0.0" h";0.0" h"', "currency": '+"$"#,##0.00;-"$"#,##0.00;"$"0.00'}
FIXED_SHEETS = ["Summary", "KPIs", "Findings", "Evidence", "Quality", "Hypotheses", "Alerts"]
MAX_COL_WIDTH = 60
MAX_CELL_CHARS = 32000  # Excel's limit is 32767
MAX_CHART_ROWS = 100_000
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="2F5D8A")
WRAP = Alignment(wrap_text=True, vertical="top")
log = logging.getLogger(__name__)
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _clean_str(s: str) -> str:
    s = ILLEGAL_CHARACTERS_RE.sub("", s)
    return s[:MAX_CELL_CHARS]


def put(ws: Worksheet, row: int, col: int, value: Any, number_format: str | None = None):
    """Write one cell safely: numbers stay numbers, everything else is text (never a formula)."""
    cell = ws.cell(row=row, column=col)
    if value is None:
        return cell
    if isinstance(value, bool):
        cell.value = value
    elif isinstance(value, int | float):
        cell.value = value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    else:
        cell.value = _clean_str(value if isinstance(value, str) else str(value))
        cell.data_type = "s"  # a leading '=', '+', '-', '@' stays literal text
    if number_format and isinstance(cell.value, int | float) and not isinstance(cell.value, bool):
        cell.number_format = number_format
    return cell


def _header(ws: Worksheet, headers: list[str], row: int = 1) -> None:
    for j, h in enumerate(headers, 1):
        c = put(ws, row, j, h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _table(ws: Worksheet, headers: list[str], rows: list[list[Any]], formats: dict[int, str] | None = None) -> None:
    _header(ws, headers)
    for i, r in enumerate(rows, 2):
        for j, v in enumerate(r, 1):
            put(ws, i, j, v, (formats or {}).get(j))
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"


def _autowidth(ws: Worksheet, wrap_over: int = MAX_COL_WIDTH) -> None:
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is None:
                continue
            n = max((len(x) for x in str(c.value).split("\n")), default=0)
            if isinstance(c.value, float):
                n = min(n, 14)
            widths[c.column] = max(widths.get(c.column, 0), n)
            if n > wrap_over:
                c.alignment = WRAP
    for col, n in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = max(8, min(MAX_COL_WIDTH, n + 2))


def sheet_title(raw: str, taken: set[str]) -> str:
    """Excel-valid (<=31 chars, no []:*?/\\, no edge apostrophes) and case-insensitively unique."""
    base = _BAD_SHEET_CHARS.sub("_", _clean_str(raw or "")).strip().strip("'").strip() or "Chart"
    base = base[:31]
    name, k = base, 2
    while name.lower() in taken or name.lower() == "history":
        suffix = f" ({k})"
        name = base[: 31 - len(suffix)].rstrip() + suffix
        k += 1
    taken.add(name.lower())
    return name


# ---------------------------------------------------------------------------------------------
def _summary(ws: Worksheet, d: ReportData) -> None:
    pairs = [("Title", d.title), ("Report kind", C.KIND_LABELS.get(d.kind, d.kind)), ("Workspace", d.workspace_name),
             ("Objective", d.objective), ("Period", d.period or ""), ("Generated at", d.generated_at),
             ("Run id", d.run_id), ("Summary", "\n".join(_summary_lines(d)) or C.auto_summary(d)),
             ("Lineage", d.lineage_note), ("Caveats", "\n".join(d.caveats)), ("Note", C.CAUSATION_NOTE)]
    _header(ws, ["Field", "Value"])
    for i, (k, v) in enumerate(pairs, 2):
        put(ws, i, 1, k).font = Font(bold=True)
        put(ws, i, 2, v).alignment = WRAP
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 100


def _summary_lines(d: ReportData) -> list[str]:
    lines = []
    for b in C.parse_markdown(d.summary_markdown):
        if b.kind == "para":
            lines.append(C.inline_plain(b.items[0]))
        else:
            lines += [f"• {C.inline_plain(it)}" for it in b.items]
    return lines


def _kpis(ws: Worksheet, d: ReportData) -> None:
    _header(ws, ["Metric", "Display name", "Definition", "Value", "Previous", "Change", "Format"])
    for i, m in enumerate(d.metrics, 2):
        fmt = NUMBER_FORMATS.get(m.format, NUMBER_FORMATS["number"])
        cur, prev = C.to_number(m.value), C.to_number(m.previous_value)
        put(ws, i, 1, m.name)
        put(ws, i, 2, m.display_name)
        put(ws, i, 3, m.definition)
        put(ws, i, 4, cur if cur is not None else m.value, fmt)
        put(ws, i, 5, prev if prev is not None else m.previous_value, fmt)
        ch = C.metric_change(m)
        put(ws, i, 6, ch.delta, CHANGE_FORMATS.get(m.format))
        put(ws, i, 7, m.format)


def _fraction(c: Any) -> float | None:
    f = C.to_number(c)
    return f / 100 if f is not None and f > 1 else f


def _findings(ws: Worksheet, d: ReportData) -> None:
    items = [(i, i.change or "") for i in d.insights] + [(i, "resolved") for i in d.resolved_insights]
    rows = [[i.code, i.title, i.finding, _fraction(i.confidence), "yes" if i.verified else "no", "; ".join(i.caveats), chg]
            for i, chg in items]
    _table(ws, ["Code", "Title", "Finding", "Confidence", "Verified", "Caveats", "Change"], rows, {4: "0%"})


def _evidence(ws: Worksheet, d: ReportData) -> None:
    rows = [[i.code, q.id, q.row_count, q.result_hash or "", q.sql] for i in d.insights for q in i.evidence_queries]
    _table(ws, ["Insight", "Query id", "Row count", "Result hash", "SQL"], rows, {3: "#,##0"})


def _quality(ws: Worksheet, d: ReportData) -> None:
    rows = [[q.get("severity", ""), q.get("asset", ""), q.get("column", ""), q.get("message", "")] for q in C.sorted_quality(d)]
    _table(ws, ["Severity", "Asset", "Column", "Message"], [[_scalar(v) for v in r] for r in rows])


def _hypotheses(ws: Worksheet, d: ReportData) -> None:
    rows = [[h.get("code", ""), h.get("statement", ""), h.get("status", ""), h.get("conclusion", "")] for h in d.hypotheses]
    _table(ws, ["Code", "Statement", "Status", "Conclusion"], [[_scalar(v) for v in r] for r in rows])


def _alerts(ws: Worksheet, d: ReportData) -> None:
    rows = [[a.severity, a.title, a.message, a.metric or ""] for a in C.sorted_alerts(d)]
    _table(ws, ["Severity", "Title", "Message", "Metric"], rows)


def _scalar(v: Any) -> Any:
    if v is None or isinstance(v, str | int | float | bool):
        return v
    return str(v)


def _chart_sheet(ws: Worksheet, chart: ReportChart) -> None:
    cols = [str(c) for c in chart.columns]
    rows = C.valid_rows(chart)[:MAX_CHART_ROWS]
    if not cols:
        put(ws, 1, 1, "No data (chart has no columns).")
        return
    _table(ws, cols, [[_scalar(v) for v in r] for r in rows])
    put(ws, len(rows) + 3 if rows else 3, 1, f"{chart.title} ({chart.chart_type})").font = Font(italic=True, color="59636E")
    if not rows:
        put(ws, 2, 1, "No data.")
        return
    try:
        _native_chart(ws, chart, rows)
    except Exception as e:  # a native chart is a convenience; the data table is the deliverable
        log.warning("native Excel chart for %r skipped: %s", chart.key, e)


def _native_chart(ws: Worksheet, chart: ReportChart, rows: list[list[Any]]) -> None:
    ctype = chart.chart_type.lower()
    if ctype not in ("line", "bar", "stacked_bar", "histogram", "pie", "scatter"):
        return
    ncols = len(chart.columns)
    if ctype == "scatter":
        if ncols < 2 or not all(isinstance(r[0], int | float) and isinstance(r[1], int | float) for r in rows):
            return
        sc = ScatterChart()
        sc.style = 13
        xref = Reference(ws, min_col=1, min_row=2, max_row=len(rows) + 1)
        yref = Reference(ws, min_col=2, min_row=1, max_row=len(rows) + 1)
        s = Series(yref, xref, title_from_data=True)
        s.marker.symbol = "circle"
        s.graphicalProperties.line.noFill = True
        sc.series.append(s)
        sc.x_axis.title, sc.y_axis.title = str(chart.columns[0]), str(chart.columns[1])
        _place(ws, sc, chart, ncols)
        return
    w = C.to_wide(chart)
    if not w or not w.series:
        return
    if w.pivoted or any(not isinstance(v, int | float) for r in rows for v in r[1:] if v is not None):
        # write the wide (pivoted / numeric-coerced) block beside the raw data and chart that
        start = ncols + 2
        put(ws, 1, start, w.x_name).font = Font(bold=True)
        for j, name in enumerate(w.series, 1):
            put(ws, 1, start + j, name).font = Font(bold=True)
        for i, x in enumerate(w.x, 2):
            put(ws, i, start, x)
            for j, vals in enumerate(w.series.values(), 1):
                put(ws, i, start + j, vals[i - 2])
        cat_col, first, last, nrows = start, start + 1, start + len(w.series), len(w.x)
    else:
        num_idx = [j + 1 for j, c in enumerate(chart.columns) if str(c) in w.series]
        if num_idx != list(range(num_idx[0], num_idx[-1] + 1)):
            return
        cat_col, first, last, nrows = 1, num_idx[0], num_idx[-1], len(rows)
    cats = Reference(ws, min_col=cat_col, min_row=2, max_row=nrows + 1)
    data = Reference(ws, min_col=first, max_col=last, min_row=1, max_row=nrows + 1)
    if ctype == "pie":
        xc = PieChart()
        data = Reference(ws, min_col=first, min_row=1, max_row=nrows + 1)
    elif ctype == "line":
        xc = LineChart()
    else:
        xc = BarChart()
        xc.type = "col"
        if ctype == "stacked_bar":
            xc.grouping = "stacked"
            xc.overlap = 100
        if ctype == "histogram":
            xc.gapWidth = 10
    xc.add_data(data, titles_from_data=True)
    xc.set_categories(cats)
    _place(ws, xc, chart, last)


def _place(ws: Worksheet, xc, chart: ReportChart, last_col: int) -> None:
    xc.title = _clean_str(chart.title)[:200]
    xc.width, xc.height = 18, 9
    ws.add_chart(xc, f"{get_column_letter(last_col + 2)}2")


# ---------------------------------------------------------------------------------------------
def build_workbook(data: ReportData) -> Workbook:
    d = data
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    taken = {s.lower() for s in FIXED_SHEETS}
    _summary(ws, d)
    for name, fn in (("KPIs", _kpis), ("Findings", _findings), ("Evidence", _evidence)):
        fn(wb.create_sheet(name), d)
    for ch in d.charts:
        _chart_sheet(wb.create_sheet(sheet_title(ch.title or ch.key, taken)), ch)
    for name, fn in (("Quality", _quality), ("Hypotheses", _hypotheses), ("Alerts", _alerts)):
        fn(wb.create_sheet(name), d)
    for s in wb.worksheets:
        if s.title != "Summary":
            _autowidth(s)
    ts = C.parse_ts(d.generated_at)
    if ts:
        wb.properties.created = ts.replace(tzinfo=None)
    wb.properties.title = _clean_str(d.title)[:255]
    wb.properties.creator = "AnalystOS"
    return wb


def render_xlsx(data: ReportData) -> bytes:
    buf = io.BytesIO()
    build_workbook(data).save(buf)
    return buf.getvalue()
