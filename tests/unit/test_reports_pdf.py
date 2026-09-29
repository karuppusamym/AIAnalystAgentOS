"""PDF report rendering, including embedded Unicode text extraction."""
from __future__ import annotations

import io
import re
from functools import cache

import pytest
from pypdf import PdfReader
from tests.report_sample import sample_report

from analystos.contracts.reports import ReportData, ReportInsight
from analystos.reports import render
from analystos.reports.pdf import build_pdf, pdf_text, render_pdf


def _text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


@cache
def _doc(kind: str) -> tuple[int, bytes]:
    doc = build_pdf(sample_report(kind))
    return doc.pages_count, bytes(doc.output())


@pytest.mark.parametrize("kind", ["executive", "operational", "statistical", "exception", "weekly_summary"])
def test_pdf_renders_each_kind(kind):
    n, b = _doc(kind)
    assert b.startswith(b"%PDF-") and b.rstrip().endswith(b"%%EOF")
    assert len(PdfReader(io.BytesIO(b)).pages) == n >= 2
    txt = _text(b)
    assert "ServiceNow SLA analysis — week 38" in txt
    assert [int(page) for page in re.findall(r"Page (\d+)/\d+", txt)] == list(range(1, n + 1))
    assert txt.count("run run_0f3a9c2e") == n
    assert "not proof of causation" in txt
    assert b"/Image" in b  # chart PNGs embedded


def test_pdf_sections_follow_kind():
    ex = _text(_doc("executive")[1])
    assert "Top verified findings" in ex and "Data quality issues" not in ex
    st = _text(_doc("statistical")[1])
    assert "Evidence queries" in st and "SELECT priority, AVG" in st
    assert st.index("Hypotheses and conclusions") < st.index("Findings")
    exc = _text(_doc("exception")[1])
    assert exc.index("Alerts") < exc.index("Summary")


def test_cjk_and_unicode_round_trip_through_pdf():
    assert pdf_text("a → b ≥ c … “q” — Zürich 日本語 한국어 中文 🚀") == "a → b ≥ c … “q” — Zürich 日本語 한국어 中文 ?"
    d = ReportData(title="Отчёт 報告 → ≥ …", workspace_name="東京 Zürich", objective="한국어 中文 日本語 Ω\x00\x07",
                   run_id="r", generated_at="2026-09-21T08:30:00+02:00",
                   insights=[ReportInsight(code="Ж", title="χ²", finding="√ 中文 日本語 한국어", confidence=0.5,
                                           verified=True, caveats=["€"])])
    b = render_pdf(d)
    txt = _text(b)
    assert b.startswith(b"%PDF-")
    for phrase in ("Отчёт 報告 → ≥ …", "東京 Zürich", "한국어 中文 日本語 Ω", "√ 中文 日本語 한국어"):
        assert phrase in txt
    assert "?" not in txt


def test_pdf_is_deterministic_and_dispatch():
    body, mime, ext = render(sample_report("operational"), "pdf")
    assert body == _doc("operational")[1]
    assert mime == "application/pdf" and ext == "pdf" and body.startswith(b"%PDF")
    assert b"D:20260921083000" in body  # creation date pinned to generated_at
