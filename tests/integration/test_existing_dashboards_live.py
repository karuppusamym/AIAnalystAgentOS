"""Existing-dashboard mode (N-2, BI-011/012), live against the compose Superset and the staged ServiceNow mock:
a dashboard in Superset is inspected, mapped to the workspace's staged tables, every reproducible chart is
re-executed through the real QueryGateway (reader identity, audit rows) and compared with the numbers Superset
itself serves for the chart, and an approved-style metric change round-trips through the Superset API.
Skips when Superset or the data plane is down."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle  # noqa: E402
from analystos.db.models import QueryExecution  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration


@pytest.fixture()
def superset(dp_settings):
    try:
        if httpx.get(f"{dp_settings.superset_url}/health", timeout=5).status_code != 200:
            raise httpx.HTTPError("unhealthy")
    except httpx.HTTPError:
        pytest.skip(f"Superset not reachable at {dp_settings.superset_url}/health")
    from analystos.publishing.superset import SupersetPublisher

    pub = SupersetPublisher(dp_settings)
    yield pub
    pub.close()


def _bundle(workspace_id: str, schema: str) -> PublishBundle:
    return PublishBundle(
        workspace_id=workspace_id,
        datasets=[DatasetDef(name="incidents_n2", sql=f"SELECT priority, reassignment_count, opened_at FROM {schema}.incident",
                             columns=[{"name": c} for c in ("priority", "reassignment_count", "opened_at")],
                             time_column="opened_at", source_assets=[f"{schema}.incident"])],
        metrics=[MetricDef(name="incident_count", display_name="Incidents", definition="count", sql_expression="COUNT(*)"),
                 MetricDef(name="avg_reassign", display_name="Avg reassignments", definition="avg",
                           sql_expression="AVG(reassignment_count)")],
        charts=[ChartSpec(key="n2_kpi", title="Incidents", chart_type="kpi", intent="kpi", dataset="incidents_n2",
                          metric="incident_count"),
                ChartSpec(key="n2_by_priority", title="Reassignments by priority", chart_type="bar", intent="comparison",
                          dataset="incidents_n2", metric="avg_reassign", dimension="priority"),
                ChartSpec(key="n2_weekly", title="Weekly incidents", chart_type="line", intent="trend", dataset="incidents_n2",
                          metric="incident_count", time_grain="week"),
                ChartSpec(key="n2_hist", title="Reassignment spread", chart_type="histogram", intent="distribution",
                          dataset="incidents_n2", dimension="reassignment_count")],
        dashboards=[DashboardSpec(key="n2", title="N-2 existing dashboard (test)", audience="operational",
                                  charts=["n2_kpi", "n2_by_priority", "n2_weekly", "n2_hist"])],
    )


def test_live_import_reexecutes_charts_through_the_gateway(superset, dp_settings, dp_session_factory, staged_servicenow,
                                                           servicenow_scope, dp_workspace):
    from analystos.gateway.service import QueryGateway
    from analystos.services import existing_dashboards as svc

    ws = dp_workspace["workspace_id"]
    published = superset.publish(_bundle(ws, staged_servicenow["schema"]), idempotency_key=f"it-n2-{ws}")
    assert published.status == "succeeded", published.errors
    try:
        dash_id = published.external_ids["dashboards"]["n2"]
        assert dash_id in {d["id"] for d in superset.list_dashboards(ws)}
        inspection = superset.inspect_dashboard(dash_id, workspace_id=ws)
        # a staged source is the workspace's own analytics database in Superset
        datasets = svc.map_datasets(inspection, servicenow_scope,
                                    {staged_servicenow["source_id"]: {svc.workspace_database(ws)}})
        (mapped,) = datasets.values()
        assert mapped["mapped"] and mapped["owned"] and mapped["assets"] == [f"{staged_servicenow['schema']}.incident"]
        assert not svc.map_datasets(inspection, servicenow_scope, {})[str(inspection["datasets"][0]["id"])]["mapped"]

        gateway = QueryGateway(dp_settings, session_factory=dp_session_factory)
        results = svc.verify_charts(inspection, datasets, servicenow_scope, gateway=gateway, source=superset,
                                    actor=f"user:{dp_workspace['user_id']}")
        by_key = {next(c["aos_key"] for c in inspection["charts"] if c["id"] == r["chart_id"]): r for r in results}
        assert by_key["n2_hist"]["status"] == "not_reproducible"
        for key in ("n2_kpi", "n2_by_priority", "n2_weekly"):
            assert by_key[key]["status"] == "verified", (key, by_key[key])
            assert by_key[key]["comparison"]["compared"] > 0
        with dp_session_factory() as s:
            audited = s.scalars(select(QueryExecution).where(QueryExecution.id.in_(
                [r["query_id"] for r in results if r.get("query_id")]))).all()
        assert len(audited) == 3 and {q.purpose for q in audited} == {svc.PURPOSE}

        ds_id = inspection["datasets"][0]["id"]
        superset.apply_changes(inspection, [{"type": "set_dataset_metric", "dataset_id": str(ds_id), "metric": "avg_reassign",
                                             "expression": "AVG(reassignment_count)", "certify": True}])
        after = superset.inspect_dashboard(dash_id, workspace_id=ws)
        certified = {m["name"]: m["certified"] for m in after["datasets"][0]["metrics"]}
        assert certified["avg_reassign"] is True
        assert svc.fingerprint(after) != svc.fingerprint(inspection)  # a write shows up as drift on the next import
    finally:
        superset.rollback(published.external_ids)
