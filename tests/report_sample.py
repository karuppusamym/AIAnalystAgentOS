"""A realistic ReportData (ServiceNow SLA analysis) for renderer tests.

It deliberately carries hostile/awkward content — HTML/script payloads, a javascript: link,
formula-injection strings, non-latin text, an empty chart and a malformed chart — so every renderer
is exercised against the input it must survive.
"""
from __future__ import annotations

from analystos.contracts.reports import (
    ReportAlert,
    ReportChart,
    ReportData,
    ReportInsight,
    ReportMetric,
    ReportQuery,
)

XSS = "<script>alert('x')</script>"


def sample_report(kind: str = "executive") -> ReportData:
    q = lambda i, sql, n, h: ReportQuery(id=i, sql=sql, row_count=n, result_hash=h)  # noqa: E731
    insights = [
        ReportInsight(
            code="F-001", title="P1 incidents breach SLA far more often",
            finding="P1 incidents met SLA 71.2% of the time vs 91.4% for P3 (n=1,284).",
            confidence=0.92, verified=True, change="persisting",
            caveats=["Priority is set at intake and may be revised later."],
            evidence_queries=[q("q-sla-by-priority", 'SELECT priority, AVG(CASE WHEN made_sla THEN 1.0 ELSE 0.0 END) AS sla_rate\n'
                                "FROM src_sn.incident GROUP BY priority ORDER BY priority", 4, "sha256:9f2c1a")],
        ),
        ReportInsight(
            code="F-002", title="Network group resolution time rose " + "≥ 20% → week over week",
            finding=f"Median resolution for Network rose to 14.2 h. {XSS}", confidence=0.81, verified=True, change="new",
            caveats=["Holiday week — lower staffing…", "Zürich site data is partial (日本語 labels)."],
            evidence_queries=[q("q-network-res", "SELECT assignment_group, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY resolution_hours)\n"
                                "FROM src_sn.incident WHERE assignment_group = 'Network' GROUP BY 1", 1, "sha256:44ab01")],
        ),
        ReportInsight(
            code="F-003", title="=HYPERLINK(\"http://evil.example\",\"click\")", finding="+cmd|' /C calc'!A0",
            confidence=0.64, verified=False, change="changed", caveats=["@SUM(1+1)*cmd|' /C calc'!A0"],
        ),
        ReportInsight(
            code="F-004", title="Reopened tickets cluster on Mondays", finding="18% of reopenings happen on Monday.",
            confidence=0.77, verified=True, change=None,
            evidence_queries=[q("q-reopen-dow", "SELECT EXTRACT(dow FROM reopened_at) d, COUNT(*) FROM src_sn.incident GROUP BY 1", 7, None)],
        ),
        ReportInsight(
            code="F-005", title="Email channel has the slowest first response",
            finding="Email-opened incidents wait 3.1 h for first response vs 0.4 h via portal.",
            confidence=0.88, verified=True, change="new",
            evidence_queries=[q("q-first-resp", "SELECT contact_type, AVG(first_response_hours) FROM src_sn.incident GROUP BY 1", 5, "sha256:77e0c2")],
        ),
    ]
    resolved = [ReportInsight(code="F-000", title="Backlog spike in Service Desk", finding="Resolved since last run.",
                              confidence=0.7, verified=True)]
    metrics = [
        ReportMetric(name="sla_rate", display_name="SLA attainment", definition="Share of incidents resolved within SLA",
                     value=0.873, previous_value=0.891, format="percent"),
        ReportMetric(name="p1_sla_rate", display_name="P1 SLA attainment", definition="SLA attainment for P1",
                     value=0.712, previous_value=0.698, format="percent"),
        ReportMetric(name="mttr", display_name="Mean time to resolve", definition="AVG(resolution_hours)",
                     value=11.46, previous_value=10.2, format="hours"),
        ReportMetric(name="first_response", display_name="First response", definition="AVG(first_response_hours)",
                     value=1.25, previous_value=None, format="hours"),
        ReportMetric(name="incident_count", display_name="Incidents", definition="COUNT(*)", value=1284, previous_value=1310),
        ReportMetric(name="backlog", display_name="Open backlog", definition="-SUM(open) formula bait", value="n/a", previous_value=212),
    ]
    charts = [
        ReportChart(key="sla_trend", title="SLA attainment by week", chart_type="line", columns=["week", "sla_rate"],
                    rows=[[f"2026-W{w:02d}", 0.85 + (w % 5) / 100] for w in range(28, 39)]),
        ReportChart(key="res_by_group", title="Median resolution by group [hours]: top/bottom?", chart_type="bar",
                    columns=["assignment_group", "median_hours"],
                    rows=[["Network", 14.2], ["Service Desk", 6.1], ["Database", 9.8], ["=cmd()", 2.0]]),
        ReportChart(key="by_priority", title="Incidents by priority", chart_type="pie", columns=["priority", "count"],
                    rows=[["P1", 84], ["P2", 310], ["P3", 640], ["P4", 250]]),
        ReportChart(key="empty", title="Reopen rate by channel", chart_type="heatmap", columns=["channel", "dow", "rate"], rows=[]),
        ReportChart(key="broken", title="Malformed resolution histogram", chart_type="histogram", columns=["bin", "count"],
                    rows=[["0-4"], [None, None, None], [{"x": 1}, "abc"]]),
    ]
    return ReportData(
        kind=kind,  # type: ignore[arg-type]
        title="ServiceNow SLA analysis — week 38",
        workspace_name="IT Ops <b>Europe</b>",
        objective="Explain why SLA attainment dropped and where resolution time is growing.",
        run_id="run_0f3a9c2e",
        generated_at="2026-09-21T08:30:00Z",
        period="2026-09-15 .. 2026-09-21",
        summary_markdown=(
            "SLA attainment fell **1.8 pp** to _87.3%_; the drop is concentrated in `Network`.\n\n"
            "- P1 breaches persist\n- Email first response is slow\n"
            "- See [runbook](https://wiki.example.com/sla) and [bad](javascript:alert(1))\n\n"
            f"{XSS} <img src=x onerror=alert(1)>"
        ),
        insights=insights,
        resolved_insights=resolved,
        metrics=metrics,
        charts=charts,
        quality_issues=[
            {"severity": "warning", "asset": "src_sn.incident", "column": "resolved_at", "message": "3.2% nulls on closed incidents"},
            {"severity": "critical", "asset": "src_sn.incident", "column": "priority", "message": f"Mixed case values {XSS}"},
            {"severity": "info", "asset": "src_sn.sys_user", "column": None, "message": "=1+1 looks like a formula"},
        ],
        hypotheses=[
            {"code": "H-1", "statement": "P1 incidents breach SLA more than P3", "status": "supported", "conclusion": "Difference 20.2 pp, p<0.001"},
            {"code": "H-2", "statement": "Weekend-opened incidents resolve slower", "status": "not_supported", "conclusion": "No significant difference"},
            {"code": "H-3", "statement": "Email channel delays first response", "status": "supported", "conclusion": None},
        ],
        alerts=[
            ReportAlert(severity="warning", title="MTTR above threshold", message="MTTR 11.5 h > 10 h target", metric="mttr"),
            ReportAlert(severity="critical", title="SLA below 88%", message=f"SLA attainment 87.3% {XSS}", metric="sla_rate"),
        ],
        lineage_note="Computed from src_sn.incident via the query gateway; 1,284 rows, snapshot 2026-09-21.",
        caveats=["Associations only.", "Data after 2026-09-21 06:00 UTC not included."],
    )
