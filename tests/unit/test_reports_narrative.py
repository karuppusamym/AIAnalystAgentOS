"""Markdown/HTML report rendering: sections per kind, escaping of untrusted data, determinism."""
from __future__ import annotations

import re

import pytest
from tests.report_sample import sample_report

from analystos.contracts.reports import ReportData, ReportMetric
from analystos.core.errors import InvalidInput
from analystos.reports import render
from analystos.reports._common import CAUSATION_NOTE, format_value, metric_change, parse_markdown
from analystos.reports.narrative import render_html, render_markdown

KINDS = ["executive", "operational", "statistical", "exception", "weekly_summary"]


def _h2_order(md: str) -> list[str]:
    return re.findall(r"^## (.+)$", md, flags=re.M)


@pytest.mark.parametrize("kind", KINDS)
def test_markdown_sections_and_footer(kind):
    md = render_markdown(sample_report(kind))
    heads = _h2_order(md)
    assert heads[-1] == "Provenance"
    assert CAUSATION_NOTE in md
    assert "run\\_0f3a9c2e" in md or "`run_0f3a9c2e`" in md
    assert "Computed from src\\_sn.incident" in md  # lineage note
    assert "Associations only." in md
    expected_first = {"executive": "Summary", "operational": "Summary", "statistical": "Summary",
                      "exception": "Alerts", "weekly_summary": "What changed"}[kind]
    assert heads[0] == expected_first


def test_executive_sections_and_top_verified_findings():
    md = render_markdown(sample_report("executive"))
    heads = _h2_order(md)
    assert heads[:5] == ["Summary", "Key metrics", "Top verified findings", "Alerts", "What changed"]
    assert "Data quality issues" not in heads and "Hypotheses" not in heads
    # unverified F-003 is not a top verified finding; ordering is by confidence
    findings = md.split("## Top verified findings")[1].split("## Alerts")[0]
    assert "F-003" not in findings
    assert [m for m in re.findall(r"### (F-\d+)", findings)] == ["F-001", "F-005", "F-002", "F-004"]
    assert "confidence 92%" in findings and "Priority is set at intake" in findings
    # KPI table with previous value and change arrow
    assert "| SLA attainment | 87.3% | 89.1% | ↓ -1.8 pp |" in md
    assert "| Mean time to resolve | 11.5 h | 10.2 h | ↑ +1.3 h (+12.4%) |" in md
    # what changed: new / persisting / resolved
    changed = md.split("## What changed")[1]
    assert "**New (2)**" in changed and "**Persisting (1)**" in changed and "**Resolved (1)**" in changed
    assert "F-000" in changed


def test_operational_adds_quality_hypotheses_charts():
    heads = _h2_order(render_markdown(sample_report("operational")))
    for h in ("Data quality issues", "Hypotheses", "Chart data"):
        assert h in heads
    md = render_markdown(sample_report("operational"))
    assert "| critical | src\\_sn.incident | priority |" in md  # quality sorted by severity
    assert "not\\_supported" in md
    assert "| 2026-W38 | 0.88 |" in md
    assert "_No data._" in md  # empty chart


def test_statistical_has_evidence_sql_blocks():
    md = render_markdown(sample_report("statistical"))
    heads = _h2_order(md)
    assert heads[:4] == ["Summary", "Hypotheses and conclusions", "Findings", "Evidence queries"]
    assert "```sql\nSELECT priority, AVG(CASE WHEN made_sla" in md
    assert "Evidence query `q-sla-by-priority` (4 rows, hash `sha256:9f2c1a`)" in md
    assert "Difference 20.2 pp, p&lt;0.001" in md


def test_exception_alerts_first_sorted_by_severity():
    md = render_markdown(sample_report("exception"))
    assert _h2_order(md)[0] == "Alerts"
    alerts = md.split("## Alerts")[1].split("##")[0]
    assert alerts.index("CRITICAL") < alerts.index("WARNING")


def test_weekly_summary_changes_then_kpis():
    assert _h2_order(render_markdown(sample_report("weekly_summary")))[:2] == ["What changed", "Key metrics"]


def test_markdown_escapes_untrusted_payloads():
    md = render_markdown(sample_report("operational"))
    assert "<script" not in md.lower() and "<img" not in md.lower() and "<b>" not in md
    assert "&lt;script&gt;" in md
    assert "javascript:" not in md  # unsafe link dropped to its label
    assert "[runbook](https://wiki.example.com/sla)" in md
    assert "**1.8 pp**" in md and "_87.3%_" in md and "`Network`" in md


@pytest.mark.parametrize("kind", KINDS)
def test_html_escapes_everything(kind):
    h = render_html(sample_report(kind))
    assert h.startswith("<!DOCTYPE html>")
    low = h.lower()
    assert "<script" not in low
    assert "<img" not in low
    assert "javascript:" not in low
    assert "onerror=" not in low or "onerror=alert(1)&gt;" in low  # only as escaped text
    assert "&lt;script&gt;" in h
    assert "IT Ops &lt;b&gt;Europe&lt;/b&gt;" in h
    assert '<a href="https://wiki.example.com/sla" rel="noopener noreferrer nofollow">runbook</a>' in h
    assert "<strong>1.8 pp</strong>" in h and "<em>87.3%</em>" in h and "<code>Network</code>" in h
    assert CAUSATION_NOTE in h and "run_0f3a9c2e" in h and "Associations only." in h
    assert "<style>" in h and "@media print" in h and "http://" not in h.split("<body>")[0]  # no external assets


def test_html_sections_per_kind():
    ex = render_html(sample_report("executive"))
    assert "Top verified findings" in ex and "sec-quality" not in ex and "sec-hypotheses" not in ex
    op = render_html(sample_report("operational"))
    assert all(f"sec-{k}" in op for k in ("quality", "hypotheses", "charts"))
    st = render_html(sample_report("statistical"))
    assert '<pre><code class="language-sql">SELECT priority' in st and "sec-evidence" in st
    assert st.index("sec-hypotheses") < st.index("sec-findings")
    exc = render_html(sample_report("exception"))
    assert exc.index("sec-alerts") < exc.index("sec-summary")
    wk = render_html(sample_report("weekly_summary"))
    assert wk.index("sec-changes") < wk.index("sec-kpis") < wk.index("sec-summary")
    assert 'class="chg down"' in ex and "↓ -1.8 pp" in ex


def test_safe_markdown_subset():
    blocks = parse_markdown("Hi **b** _i_ `c` [x](JavaScript:alert(1)) [y](ftp://a) [z](http://ok.example/a_(b))\n\n- one\n- two")
    assert [b.kind for b in blocks] == ["para", "list"]
    h = render_html(ReportData(title="t", workspace_name="w", objective="o", run_id="r", generated_at="2026-01-01T00:00:00Z",
                               summary_markdown="Hi **b** [x](JavaScript:alert(1)) [y](ftp://a) [z](http://ok.example/a_(b))\n\n<div>raw</div>"))
    assert "javascript" not in h.lower() and 'href="ftp' not in h
    assert 'href="http://ok.example/a_(b)"' in h
    assert "&lt;div&gt;raw&lt;/div&gt;" in h


def test_empty_report_renders():
    d = ReportData(title="Empty", workspace_name="w", objective="o", run_id="r1", generated_at="not a timestamp")
    for kind in KINDS:
        d2 = d.model_copy(update={"kind": kind})
        md, h = render_markdown(d2), render_html(d2)
        assert "Provenance" in md and CAUSATION_NOTE in h
        assert "This run produced 0 findings" in md


def test_value_formatting_and_change():
    assert format_value(0.873, "percent") == "87.3%"
    assert format_value(11.46, "hours") == "11.5 h"
    assert format_value(1284, "number") == "1,284"
    assert format_value(None, "number") == "n/a"
    assert format_value("n/a", "percent") == "n/a"
    ch = metric_change(ReportMetric(name="x", display_name="X", value=0.5, previous_value=0.5, format="percent"))
    assert ch.arrow == "→" and ch.text == "+0.0 pp"
    assert metric_change(ReportMetric(name="x", display_name="X", value=3, previous_value=None)).text == "n/a"


def test_determinism_and_render_dispatch():
    d = sample_report("operational")
    assert render_markdown(d) == render_markdown(sample_report("operational"))
    assert render_html(d) == render_html(sample_report("operational"))
    body, mime, ext = render(d, "md")
    assert mime.startswith("text/markdown") and ext == "md" and body.decode() == render_markdown(d)
    body, mime, ext = render(d, "html")
    assert mime.startswith("text/html") and ext == "html"
    with pytest.raises(InvalidInput):
        render(d, "docx")  # type: ignore[arg-type]
