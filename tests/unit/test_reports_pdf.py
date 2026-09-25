"""PDF report rendering with fpdf2 (pypdf is not a dependency: streams are inflated with zlib)."""
from __future__ import annotations

import re
import zlib
from functools import cache

import pytest
from tests.report_sample import sample_report

from analystos.contracts.reports import ReportData, ReportInsight
from analystos.reports import render
from analystos.reports.pdf import build_pdf, pdf_text, render_pdf


def _text(pdf: bytes) -> str:
    """Concatenated inflated content streams (core-font text appears as literal strings)."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, flags=re.S):
        try:
            out.append(zlib.decompress(m.group(1)).decode("latin-1"))
        except zlib.error:
            out.append(m.group(1).decode("latin-1"))
    return "\n".join(out)


@cache
def _doc(kind: str) -> tuple[int, bytes]:
    doc = build_pdf(sample_report(kind))
    return doc.pages_count, bytes(doc.output())


@pytest.mark.parametrize("kind", ["executive", "operational", "statistical", "exception", "weekly_summary"])
def test_pdf_renders_each_kind(kind):
    n, b = _doc(kind)
    assert b.startswith(b"%PDF-") and b.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page\b", b)) == n >= 2
    txt = _text(b)
    assert "ServiceNow SLA analysis - week 38" in txt  # title, em dash folded
    # footer with page numbers on every page ("{nb}" alias is substituted as its own text run)
    assert re.findall(r"\(Page (\d+)/\) Tj \((\d+)\) Tj", txt) == [(str(i), str(n)) for i in range(1, n + 1)]
    assert txt.count("run run_0f3a9c2e") == n
    assert "not proof of causation" in txt
    assert "/Image" in b.decode("latin-1")  # chart PNGs embedded


def test_pdf_sections_follow_kind():
    ex = _text(_doc("executive")[1])
    assert "Top verified findings" in ex and "Data quality issues" not in ex
    st = _text(_doc("statistical")[1])
    assert "Evidence queries" in st and "SELECT priority, AVG" in st
    assert st.index("Hypotheses and conclusions") < st.index("Findings")
    exc = _text(_doc("exception")[1])
    assert exc.index("Alerts") < exc.index("Summary")


def test_non_latin_text_is_sanitized_not_fatal():
    assert pdf_text("a → b ≥ c … “q” — Zürich 日本語 🚀") == 'a -> b >= c ... "q" - Zürich ??? ?'
    d = ReportData(title="Отчёт 報告 → ≥ … 🚀", workspace_name="ws​", objective="Ω\x00\x07", run_id="r",
                   generated_at="2026-09-21T08:30:00+02:00",
                   insights=[ReportInsight(code="Ж", title="χ²", finding="√ 😀", confidence=0.5, verified=True, caveats=["€"])])
    b = render_pdf(d)
    assert b.startswith(b"%PDF-")
    assert "-> >= ..." in _text(b)


def test_pdf_is_deterministic_and_dispatch():
    body, mime, ext = render(sample_report("operational"), "pdf")
    assert body == _doc("operational")[1]
    assert mime == "application/pdf" and ext == "pdf" and body.startswith(b"%PDF")
    assert b"D:20260921083000" in body  # creation date pinned to generated_at
