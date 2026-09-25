"""PDF rendering of ReportData with fpdf2 and a core font (no font files, no network).

Core fonts are latin-1 only, so every string passes through `pdf_text`, which maps common typography
to ASCII (→ to ->, ≥ to >=, … to ...), folds accents where possible and replaces anything else with
'?'. The creation date comes from ReportData.generated_at, so the same input yields the same bytes.
"""
from __future__ import annotations

import io
import re
import unicodedata
from datetime import UTC, datetime

from fpdf import FPDF
from fpdf.fonts import FontFace

from analystos.contracts.reports import ReportData, ReportInsight
from analystos.reports import _common as C
from analystos.reports.charts import render_chart_png

_MAP = {
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "↑": "^", "↓": "v", "▲": "^", "▼": "v",
    "≥": ">=", "≤": "<=", "≠": "!=", "≈": "~", "±": "+/-", "×": "x", "−": "-", "–": "-", "—": "-",
    "…": "...", "‘": "'", "’": "'", "‚": ",", "“": '"', "”": '"', "„": '"', "•": "-", "·": "-",
    "€": "EUR", "™": "(TM)", "✓": "v", "✔": "v", "✗": "x", "✘": "x", " ": " ", "​": "",
    "\t": "    ",
}
_MAP_RE = re.compile("|".join(re.escape(k) for k in _MAP))
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def pdf_text(v: object) -> str:
    """Latin-1-safe text for fpdf core fonts; never raises."""
    s = "" if v is None else str(v)
    s = _MAP_RE.sub(lambda m: _MAP[m.group(0)], s)
    s = _CTRL.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        pass
    out = []
    for ch in s:
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            folded = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
            out.append(folded or "?")
    return "".join(out)


ACCENT = (47, 93, 138)
TEXT = (31, 35, 40)
MUTED = (89, 99, 110)
LINE = (209, 217, 224)
SOFT = (246, 248, 250)
SEV = {"critical": (207, 34, 46), "warning": (154, 103, 0), "info": ACCENT}
CHART_W, CHART_H = 900, 420


class _ReportPDF(FPDF):
    def __init__(self, data: ReportData) -> None:
        super().__init__(orientation="portrait", unit="mm", format="A4")
        self.data = data
        self.set_margins(16, 16, 16)
        self.set_auto_page_break(auto=True, margin=20)
        self.alias_nb_pages()

    def footer(self) -> None:
        self.set_y(-14)
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        left = pdf_text(f"{self.data.title} | run {self.data.run_id} | generated {C.generated_label(self.data)}")
        if self.get_string_width(left) > self.epw - 30:
            while left and self.get_string_width(left + "...") > self.epw - 30:
                left = left[:-1]
            left += "..."
        self.cell(self.epw - 30, 6, left, align="L")
        self.cell(30, 6, f"Page {self.page_no()}/{{nb}}", align="R")


# ---------------------------------------------------------------------------------------------
def _h1(pdf: _ReportPDF, text: str) -> None:
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(0, 9, pdf_text(text), new_x="LMARGIN", new_y="NEXT")


def _h2(pdf: _ReportPDF, text: str) -> None:
    if pdf.get_y() > pdf.page_break_trigger - 30:
        pdf.add_page()
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*ACCENT)
    pdf.multi_cell(0, 7, pdf_text(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*LINE)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)


def _h3(pdf: _ReportPDF, text: str) -> None:
    if pdf.get_y() > pdf.page_break_trigger - 20:
        pdf.add_page()
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.set_text_color(*TEXT)
    pdf.multi_cell(0, 5.5, pdf_text(text), new_x="LMARGIN", new_y="NEXT")


def _p(pdf: _ReportPDF, text: str, *, size: float = 9.5, style: str = "", color=TEXT, indent: float = 0) -> None:
    pdf.set_font("Helvetica", style, size)
    pdf.set_text_color(*color)
    if indent:
        pdf.set_x(pdf.l_margin + indent)
    pdf.multi_cell(pdf.epw - indent, size * 0.5, pdf_text(text), new_x="LMARGIN", new_y="NEXT")


def _bullets(pdf: _ReportPDF, items: list[str], *, size: float = 9.5, indent: float = 3) -> None:
    for it in items:
        _p(pdf, f"- {it}", size=size, indent=indent)


def _code(pdf: _ReportPDF, code: str) -> None:
    pdf.set_font("Courier", "", 7.5)
    pdf.set_text_color(*TEXT)
    pdf.set_fill_color(*SOFT)
    pdf.set_draw_color(*LINE)
    pdf.multi_cell(0, 3.6, pdf_text(code.rstrip()), border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)


def _table(pdf: _ReportPDF, headers: list[str], rows: list[list[str]], widths: tuple[float, ...] | None = None,
           size: float = 8.5) -> None:
    if not rows:
        return
    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(*TEXT)
    pdf.set_draw_color(*LINE)
    pdf.set_fill_color(255, 255, 255)
    head = FontFace(emphasis="BOLD", fill_color=SOFT, color=TEXT)
    with pdf.table(col_widths=widths, text_align="LEFT", line_height=size * 0.5, first_row_as_headings=True,
                   headings_style=head, borders_layout="ALL", padding=1.2) as t:
        r = t.row()
        for h in headers:
            r.cell(pdf_text(h))
        for row in rows:
            r = t.row()
            for c in row:
                r.cell(pdf_text(c))
    pdf.ln(2)


def _insight(pdf: _ReportPDF, i: ReportInsight, with_evidence: bool) -> None:
    tag = f" [{i.change.upper()}]" if i.change else ""
    _h3(pdf, f"{i.code}  {i.title}{tag}")
    _p(pdf, f"{'Verified' if i.verified else 'Unverified'} - confidence {C.confidence_pct(i.confidence)}", size=8, color=MUTED)
    _p(pdf, i.finding)
    if i.caveats:
        _p(pdf, "Caveats:", size=8.5, style="B")
        _bullets(pdf, i.caveats, size=8.5)
    if i.evidence_queries:
        if with_evidence:
            for q in i.evidence_queries:
                h = f", hash {q.result_hash}" if q.result_hash else ""
                _p(pdf, f"Evidence query {q.id} ({q.row_count} rows{h})", size=8, color=MUTED)
                _code(pdf, q.sql)
        else:
            _p(pdf, "Evidence: " + ", ".join(q.id for q in i.evidence_queries), size=8, color=MUTED)
    pdf.ln(2)


def _section(pdf: _ReportPDF, key: str, d: ReportData) -> None:
    kind = d.kind
    _h2(pdf, C.section_title(key, kind))
    if key == "summary":
        if not d.summary_markdown.strip():
            _p(pdf, C.auto_summary(d))
        for b in C.parse_markdown(d.summary_markdown):
            if b.kind == "para":
                _p(pdf, C.inline_plain(b.items[0]))
                pdf.ln(1.5)
            else:
                _bullets(pdf, [C.inline_plain(it) for it in b.items])
                pdf.ln(1.5)
    elif key == "kpis":
        if not d.metrics:
            _p(pdf, "No metrics in this run.")
            return
        rows = []
        for m in d.metrics:
            ch = C.metric_change(m)
            rows.append([m.display_name, C.format_value(m.value, m.format), C.format_value(m.previous_value, m.format),
                         f"{ch.arrow} {ch.text}".strip(), m.definition])
        _table(pdf, ["Metric", "Value", "Previous", "Change", "Definition"], rows, (30, 18, 18, 26, 58))
    elif key == "findings":
        items = C.top_verified(d.insights) if kind == "executive" else d.insights
        if not items:
            _p(pdf, "No verified findings in this run." if kind == "executive" else "No findings in this run.")
        for i in items:
            _insight(pdf, i, kind == "statistical")
    elif key == "alerts":
        if not d.alerts:
            _p(pdf, "No alerts.")
        for a in C.sorted_alerts(d):
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.set_text_color(*SEV.get(a.severity, MUTED))
            pdf.multi_cell(0, 5, pdf_text(f"{a.severity.upper()}  {a.title}"), new_x="LMARGIN", new_y="NEXT")
            _p(pdf, a.message + (f" (metric: {a.metric})" if a.metric else ""), indent=3)
            pdf.ln(1)
    elif key == "changes":
        g = C.change_groups(d)
        if not any(g.values()):
            _p(pdf, "No comparison with a previous run is available.")
            return
        for k, lab in (("new", "New"), ("changed", "Changed"), ("persisting", "Persisting"), ("resolved", "Resolved")):
            _p(pdf, f"{lab} ({len(g[k])})", style="B")
            _bullets(pdf, [f"{i.code}  {i.title}" for i in g[k]] or ["none"])
            pdf.ln(1)
    elif key == "quality":
        if not d.quality_issues:
            _p(pdf, "No data quality issues recorded.")
            return
        _table(pdf, ["Severity", "Asset", "Column", "Issue"],
               [[str(q.get("severity", "")), str(q.get("asset", "")), str(q.get("column", "") or ""), str(q.get("message", ""))]
                for q in C.sorted_quality(d)], (18, 36, 30, 66))
    elif key == "hypotheses":
        if not d.hypotheses:
            _p(pdf, "No hypotheses were tested.")
            return
        _table(pdf, ["Code", "Hypothesis", "Status", "Conclusion"],
               [[str(h.get("code", "")), str(h.get("statement", "")), str(h.get("status", "")), str(h.get("conclusion", "") or "")]
                for h in d.hypotheses], (16, 62, 20, 52))
    elif key == "evidence":
        qs = [(i.code, q) for i in d.insights for q in i.evidence_queries]
        if not qs:
            _p(pdf, "No evidence queries recorded.")
            return
        _table(pdf, ["Finding", "Query", "Rows", "Result hash"], [[c, q.id, str(q.row_count), q.result_hash or ""] for c, q in qs],
               (22, 40, 16, 72))
    elif key == "charts":
        if not d.charts:
            _p(pdf, "No charts in this run.")
        for ch in d.charts:
            rows = C.valid_rows(ch)
            _h3(pdf, ch.title)
            if not rows:
                _p(pdf, "No data.", size=8.5, color=MUTED)
                continue
            _table(pdf, [str(c) for c in ch.columns], [[C.cell_text(v) for v in r] for r in rows[: C.MAX_TABLE_ROWS]], size=7.5)
            if len(rows) > C.MAX_TABLE_ROWS:
                _p(pdf, f"Showing {C.MAX_TABLE_ROWS} of {len(rows)} rows.", size=8, color=MUTED)


def _charts(pdf: _ReportPDF, d: ReportData) -> None:
    images = [(ch, png) for ch in d.charts if (png := render_chart_png(ch, width_px=CHART_W, height_px=CHART_H))]
    if not images:
        return
    h = pdf.epw * CHART_H / CHART_W
    if pdf.get_y() + h + 16 > pdf.page_break_trigger:
        pdf.add_page()
    _h2(pdf, "Charts")
    for _, png in images:
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page()
        pdf.image(io.BytesIO(png), x=pdf.l_margin, w=pdf.epw, h=h)
        pdf.ln(3)


def _provenance(pdf: _ReportPDF, d: ReportData) -> None:
    _h2(pdf, C.SECTION_TITLES["provenance"])
    _p(pdf, f"Run: {d.run_id}", size=9)
    _p(pdf, f"Generated: {C.generated_label(d)}", size=9)
    if d.lineage_note:
        _p(pdf, f"Lineage: {d.lineage_note}", size=9)
    if d.caveats:
        _p(pdf, "Caveats:", size=9, style="B")
        _bullets(pdf, d.caveats, size=9)
    pdf.ln(2)
    _p(pdf, C.CAUSATION_NOTE, size=9, style="I")


def build_pdf(data: ReportData) -> FPDF:
    """The laid-out document (exposed so callers/tests can inspect page count before output)."""
    d = data
    pdf = _ReportPDF(d)
    created = C.parse_ts(d.generated_at) or datetime(1970, 1, 1, tzinfo=UTC)
    pdf.set_creation_date(created)
    pdf.set_title(pdf_text(d.title))
    pdf.set_subject(pdf_text(C.KIND_LABELS.get(d.kind, d.kind)))
    pdf.set_author(pdf_text(d.workspace_name))
    pdf.set_creator("AnalystOS")
    pdf.add_page()

    pdf.set_fill_color(*ACCENT)
    pdf.rect(pdf.l_margin, pdf.t_margin, pdf.epw, 1.6, style="F")
    pdf.ln(5)
    _p(pdf, C.KIND_LABELS.get(d.kind, d.kind).upper(), size=8.5, style="B", color=ACCENT)
    _h1(pdf, d.title)
    meta = f"{d.workspace_name}  |  generated {C.generated_label(d)}" + (f"  |  period {d.period}" if d.period else "")
    _p(pdf, meta, size=9, color=MUTED)
    pdf.ln(1)
    _p(pdf, f"Objective: {d.objective}", size=9.5)
    pdf.ln(2)

    for key in C.sections_for(d.kind):
        _section(pdf, key, d)
    _charts(pdf, d)
    _provenance(pdf, d)
    return pdf


def render_pdf(data: ReportData) -> bytes:
    return bytes(build_pdf(data).output())
