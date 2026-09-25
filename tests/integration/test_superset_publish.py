"""Live publish into the docker-compose Superset (4.1.x) + Postgres analytics DB.

Proves charts render (their stored query_context returns rows), that a replay with the same
idempotency key creates no duplicates, and that rollback removes what was created.
"""
from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle
from analystos.core.config import get_settings
from analystos.publishing.superset import SupersetPublisher

pytestmark = pytest.mark.integration

SCHEMA = "src_publishtest"
WORKSPACE = "ws_publishtest"
COLUMNS = ["opened_at", "priority", "assignment_group", "reassignment_count", "made_sla", "resolution_hours"]


def _superset_up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    if not _superset_up(s.superset_url):
        pytest.skip(f"Superset not reachable at {s.superset_url}/health")
    try:
        import psycopg

        psycopg.connect(s.analytics_loader_url.replace("postgresql+psycopg://", "postgresql://"), connect_timeout=5).close()
    except Exception as exc:  # noqa: BLE001 - any connect failure means skip
        pytest.skip(f"analytics Postgres not reachable: {exc}")
    return s


@pytest.fixture(scope="module")
def incidents_table(settings):
    import psycopg

    rng = random.Random(42)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for _ in range(500):
        priority = rng.choice(["1 - Critical", "2 - High", "3 - Moderate", "4 - Low"])
        hours = round(rng.lognormvariate(2.5 if priority.startswith(("3", "4")) else 1.8, 0.7), 2)
        rows.append((
            start + timedelta(hours=rng.randint(0, 24 * 180)),
            priority,
            rng.choice(["Service Desk", "Network", "Database", "Applications", "Security"]),
            rng.choice([0, 0, 0, 1, 1, 2, 3]),
            hours < 24,
            hours,
        ))
    dsn = settings.analytics_loader_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.execute(f"DROP TABLE IF EXISTS {SCHEMA}.incidents")
        conn.execute(
            f"CREATE TABLE {SCHEMA}.incidents (opened_at timestamptz, priority text, assignment_group text, "
            "reassignment_count int, made_sla boolean, resolution_hours double precision)"
        )
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO {SCHEMA}.incidents VALUES (%s,%s,%s,%s,%s,%s)", rows)
        conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO analystos_reader")
        conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO analystos_reader")
    return f"{SCHEMA}.incidents"


def make_bundle(table: str) -> PublishBundle:
    return PublishBundle(
        workspace_id=WORKSPACE,
        datasets=[DatasetDef(
            name="incidents",
            description="Incident snapshot for publish integration test",
            sql=f"SELECT {', '.join(COLUMNS)} FROM {table}",
            columns=[{"name": c} for c in COLUMNS],
            time_column="opened_at",
            source_assets=[table],
        )],
        metrics=[
            MetricDef(name="incident_count", display_name="Incidents", definition="Number of incidents",
                      sql_expression="COUNT(*)"),
            MetricDef(name="sla_rate", display_name="SLA attainment", definition="Share resolved within SLA",
                      sql_expression="AVG(CASE WHEN made_sla THEN 1.0 ELSE 0 END)", format="percent"),
            MetricDef(name="avg_resolution_hours", display_name="Avg resolution (h)", definition="Mean hours to resolve",
                      sql_expression="AVG(resolution_hours)", format="hours", source_columns=["resolution_hours"]),
        ],
        charts=[
            ChartSpec(key="kpi_incidents", title="Incidents", chart_type="kpi", intent="kpi", dataset="incidents",
                      metric="incident_count"),
            ChartSpec(key="kpi_sla", title="SLA attainment", chart_type="kpi", intent="kpi", dataset="incidents",
                      metric="sla_rate"),
            ChartSpec(key="weekly_trend", title="Incidents per week", chart_type="line", intent="trend",
                      dataset="incidents", metric="incident_count", time_grain="week"),
            ChartSpec(key="resolution_by_priority", title="Resolution hours by priority", chart_type="bar",
                      intent="comparison", dataset="incidents", metric="avg_resolution_hours", dimension="priority"),
            ChartSpec(key="group_priority_stack", title="Incidents by group and priority", chart_type="stacked_bar",
                      intent="comparison", dataset="incidents", metric="incident_count",
                      dimension="assignment_group", series="priority"),
            ChartSpec(key="resolution_hist", title="Resolution time distribution", chart_type="histogram",
                      intent="distribution", dataset="incidents", dimension="resolution_hours"),
            ChartSpec(key="priority_share", title="Priority mix", chart_type="pie", intent="part_to_whole",
                      dataset="incidents", metric="incident_count", dimension="priority"),
            ChartSpec(key="group_heatmap", title="Reassignments", chart_type="heatmap", intent="relationship",
                      dataset="incidents", metric="incident_count", dimension="assignment_group", series="priority"),
            ChartSpec(key="group_table", title="By assignment group", chart_type="table", intent="detail",
                      dataset="incidents", metrics=["incident_count", "sla_rate", "avg_resolution_hours"],
                      dimension="assignment_group"),
            ChartSpec(key="raw_table", title="Incident detail", chart_type="table", intent="detail",
                      dataset="incidents", limit=50),
            ChartSpec(key="group_treemap", title="Group x priority", chart_type="treemap", intent="part_to_whole",
                      dataset="incidents", metric="incident_count", dimension="assignment_group", series="priority"),
            ChartSpec(key="daily_scatter", title="Daily resolution", chart_type="scatter", intent="relationship",
                      dataset="incidents", metric="avg_resolution_hours", time_grain="day"),
            ChartSpec(key="group_bubble", title="Volume vs resolution", chart_type="scatter", intent="relationship",
                      dataset="incidents", metrics=["incident_count", "avg_resolution_hours", "sla_rate"],
                      dimension="assignment_group"),
            ChartSpec(key="monthly_bar_by_priority", title="Monthly by priority", chart_type="bar", intent="trend",
                      dataset="incidents", metric="incident_count", series="priority", time_grain="month"),
            ChartSpec(key="critical_reassigned", title="Critical, reassigned", chart_type="kpi", intent="kpi",
                      dataset="incidents", metric="incident_count",
                      filters=["\"priority\" = '1 - Critical'", "reassignment_count >= 1"]),
        ],
        dashboards=[
            DashboardSpec(key="executive", title="Incident overview (test)", audience="executive",
                          charts=["kpi_incidents", "kpi_sla", "weekly_trend", "resolution_by_priority"],
                          layout=[{"chart": "kpi_incidents", "row": 0, "col": 0, "width": 3},
                                  {"chart": "kpi_sla", "row": 0, "col": 1, "width": 3},
                                  {"chart": "weekly_trend", "row": 0, "col": 2, "width": 6},
                                  {"chart": "resolution_by_priority", "row": 1, "col": 0, "width": 12}],
                          native_filters=["priority"],
                          summary_markdown="**Integration test** - executive view."),
            DashboardSpec(key="operational", title="Incident operations (test)", audience="operational",
                          charts=["weekly_trend", "group_priority_stack", "resolution_hist", "priority_share",
                                  "group_heatmap", "group_table", "raw_table", "group_treemap", "daily_scatter",
                                  "group_bubble", "monthly_bar_by_priority", "critical_reassigned"],
                          native_filters=["assignment_group", "priority"]),
        ],
    )


def test_publish_render_replay_rollback(settings, incidents_table):
    pub = SupersetPublisher(settings)
    assert pub.test_connection()["ok"]
    bundle = make_bundle(incidents_table)
    first = pub.publish(bundle, idempotency_key="it-publish-1")
    try:
        assert first.status == "succeeded", first.errors
        ids = first.external_ids
        assert set(ids["charts"]) == {c.key for c in bundle.charts}
        assert set(ids["dashboards"]) == {"executive", "operational"}

        # Every chart renders: its stored query_context returns rows.
        for key, chart_id in ids["charts"].items():
            body = pub.client.get(f"/api/v1/chart/{chart_id}/data/", params={"format": "json", "type": "full"})
            result = body["result"][0]
            assert not result.get("error"), (key, result.get("error"))
            assert result["rowcount"] > 0 and result["data"], key

        # ChartSpec.filters are applied as adhoc SQL WHERE filters.
        crit = ids["charts"]["critical_reassigned"]
        sql = pub.client.get(f"/api/v1/chart/{crit}/data/", params={"format": "json", "type": "query"})["result"][0]["query"]
        assert "\"priority\" = '1 - Critical'" in sql and "reassignment_count >= 1" in sql
        filtered = pub.client.get(f"/api/v1/chart/{crit}/data/")["result"][0]["data"][0]["incident_count"]
        total = pub.client.get(f"/api/v1/chart/{ids['charts']['kpi_incidents']}/data/")["result"][0]["data"][0]["incident_count"]
        assert total == 500 and 0 < filtered < total
        params = json.loads(pub.client.get(f"/api/v1/chart/{crit}")["result"]["params"])
        assert [f["sqlExpression"] for f in params["adhoc_filters"]] == bundle.charts[-1].filters
        viz = {k: pub.client.get(f"/api/v1/chart/{cid}")["result"]["viz_type"] for k, cid in ids["charts"].items()}
        assert viz["group_bubble"] == "bubble_v2" and viz["group_treemap"] == "treemap_v2"
        assert viz["resolution_hist"] == "histogram_v2" and viz["kpi_incidents"] == "big_number_total"

        for key, dash_id in ids["dashboards"].items():
            dash = pub.client.get(f"/api/v1/dashboard/{dash_id}")["result"]
            assert dash["published"] is True
            charts = pub.client.get(f"/api/v1/dashboard/{dash_id}/charts")["result"]
            expected = next(d for d in bundle.dashboards if d.key == key).charts
            assert sorted(c["id"] for c in charts) == sorted(ids["charts"][k] for k in expected)
            assert first.urls[key].endswith(f"/superset/dashboard/{dash_id}/")

        inspected = pub.inspect_dashboard(ids["dashboards"]["executive"])
        assert len(inspected["layout"]) == 4 and inspected["filters"][0]["targets"][0]["column"] == "priority"

        # Replay (fresh publisher, no `previous`): same objects, nothing duplicated.
        replay = SupersetPublisher(settings).publish(bundle, idempotency_key="it-publish-1")
        assert replay.status == "succeeded", replay.errors
        for kind in ("database", "datasets", "charts", "dashboards"):
            assert replay.external_ids[kind] == ids[kind], kind
        prefix = pub.chart_prefix(WORKSPACE)
        assert len(pub.client.list("chart", [{"col": "slice_name", "opr": "sw", "value": prefix}])) == len(bundle.charts)
    finally:
        deleted = pub.rollback(first.external_ids)
    assert len([d for d in deleted if d.startswith("chart:")]) == len(bundle.charts)
    for kind, resource in (("charts", "chart"), ("dashboards", "dashboard"), ("datasets", "dataset")):
        for oid in first.external_ids[kind].values():
            assert pub.client.get(f"/api/v1/{resource}/{oid}", allow_404=True) is None
