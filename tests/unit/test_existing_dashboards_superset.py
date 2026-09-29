"""Existing-dashboard mode, Superset side (N-2, BI-011/012): chart definitions translate to a platform-neutral
query (or say why they cannot be reproduced), inspection is tenant-scoped, chart data is read-only, and the one
write (a dataset metric) keeps every other metric. Against the stateful in-memory Superset of the publish tests."""
from __future__ import annotations

import json

import httpx
import pytest
import respx
from tests.unit.test_publishing_superset import BASE, FakeSuperset, make_bundle, publisher

from analystos.core.errors import NotFound
from analystos.publishing.base import DashboardSource
from analystos.publishing.superset_import import chart_query


class DataSuperset(FakeSuperset):
    """Adds GET /api/v1/chart/{id}/data/ (Superset's own numbers for a saved chart)."""

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[int, dict] = {}

    def route(self, method, path, body, q):  # noqa: ANN001, ANN201
        parts = path.strip("/").split("/")
        if method == "GET" and len(parts) == 5 and parts[2] == "chart" and parts[4] == "data":
            cid = int(parts[3])
            if cid not in self.data:
                return httpx.Response(400, json={"message": "Chart has no query context saved"})
            return httpx.Response(200, json={"result": [self.data[cid]]})
        return super().route(method, path, body, q)


@pytest.fixture
def fake() -> DataSuperset:
    return DataSuperset()


@pytest.fixture
def router(fake):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.route().mock(side_effect=fake.handle)
        yield mock


def test_superset_publisher_is_a_dashboard_source():
    assert isinstance(publisher(), DashboardSource)


def test_published_charts_translate_to_reproducible_queries(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    info = publisher().inspect_dashboard(res.external_ids["dashboards"]["executive"], workspace_id="ws_unit")
    q = {c["aos_key"]: c["query"] for c in info["charts"]}
    assert q["kpi_count"]["metrics"] == [{"label": "incident_count", "expression": "COUNT(*)", "saved": True, "certified": False}]
    assert q["kpi_count"]["dimensions"] == [] and q["kpi_count"]["unsupported"] is None
    assert (q["trend"]["time_column"], q["trend"]["time_grain"]) == ("opened_at", "week")
    assert q["by_priority"]["dimensions"] == ["priority"] and q["by_priority"]["time_column"] is None
    assert q["by_priority"]["metrics"][0]["expression"] == "AVG(resolution_hours)"
    assert {d["kind"] for d in info["datasets"]} == {"virtual"}
    ops = publisher().inspect_dashboard(res.external_ids["dashboards"]["operational"], workspace_id="ws_unit")
    hist = next(c for c in ops["charts"] if c["aos_key"] == "res_hist")
    assert hist["query"]["unsupported"].startswith("a histogram_v2")


def test_another_workspaces_dashboard_is_not_found_and_not_listed(router, fake):
    res = publisher().publish(make_bundle("ws_other"), idempotency_key="k1")
    other = res.external_ids["dashboards"]["executive"]
    with pytest.raises(NotFound):
        publisher().inspect_dashboard(other, workspace_id="ws_unit")
    assert publisher().list_dashboards("ws_unit") == []
    assert {d["id"] for d in publisher().list_dashboards("ws_other")} == set(res.external_ids["dashboards"].values())


def test_chart_data_reads_superset_numbers_and_never_raises(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    cid = res.external_ids["charts"]["by_priority"]
    fake.data[cid] = {"colnames": ["priority", "avg_resolution_hours"], "data": [
        {"priority": "P1", "avg_resolution_hours": 4.5}, {"priority": "P2", "avg_resolution_hours": 9.0}]}
    assert publisher().chart_data(cid) == {"columns": ["priority", "avg_resolution_hours"], "rows": [["P1", 4.5], ["P2", 9.0]]}
    assert "error" in publisher().chart_data(res.external_ids["charts"]["trend"])
    assert all(m == "GET" for m, p in fake.calls if "/data/" in p)


def test_set_dataset_metric_changes_one_metric_and_keeps_the_rest(router, fake):
    res = publisher().publish(make_bundle(), idempotency_key="k1")
    ds_id = res.external_ids["datasets"]["incidents"]
    before = {m["metric_name"]: m for m in fake.datasets[ds_id]["metrics"]}
    info = publisher().inspect_dashboard(res.external_ids["dashboards"]["executive"], workspace_id="ws_unit")
    done = publisher().apply_changes(info, [{"type": "set_dataset_metric", "dataset_id": str(ds_id), "metric": "incident_count",
                                             "expression": "COUNT(DISTINCT number)", "certify": True, "details": "x"}])
    assert done == [f"dataset:{ds_id}:metric:incident_count"]
    after = {m["metric_name"]: m for m in fake.datasets[ds_id]["metrics"]}
    assert set(after) == set(before)
    assert after["incident_count"]["expression"] == "COUNT(DISTINCT number)"
    assert json.loads(after["incident_count"]["extra"])["certification"]["certified_by"] == "AnalystOS semantic layer"
    assert after["avg_resolution_hours"]["expression"] == before["avg_resolution_hours"]["expression"]


def test_apply_changes_refuses_a_dataset_outside_the_dashboard(router, fake):
    from analystos.core.errors import InvalidInput

    res = publisher().publish(make_bundle(), idempotency_key="k1")
    info = publisher().inspect_dashboard(res.external_ids["dashboards"]["executive"], workspace_id="ws_unit")
    with pytest.raises(InvalidInput):
        publisher().apply_changes(info, [{"type": "set_dataset_metric", "dataset_id": "999", "metric": "x", "expression": "1"}])
    with pytest.raises(InvalidInput):
        publisher().apply_changes(info, [{"type": "delete_chart", "dataset_id": "1"}])


@pytest.mark.parametrize(("form_data", "reason"), [
    ({"viz_type": "big_number_total", "metric": "m", "time_range": "Last week"}, "relative to when the chart runs"),
    ({"viz_type": "echarts_timeseries_line", "metrics": ["m"], "rolling_type": "mean"}, "rolling_type"),
    ({"viz_type": "table", "query_mode": "raw", "all_columns": ["a"]}, "raw-records"),
    ({"viz_type": "pie", "metric": "nope", "groupby": ["a"]}, "not defined on the dataset"),
    ({"viz_type": "pie", "metric": "m", "groupby": [{"expressionType": "SQL", "sqlExpression": "lower(a)"}]}, "custom SQL"),
    ({"viz_type": "pie", "metric": "m", "groupby": ["a"], "adhoc_filters": [{"clause": "HAVING", "expressionType": "SQL",
                                                                              "sqlExpression": "m > 1"}]}, "HAVING"),
])
def test_unreproducible_charts_say_why(form_data, reason):
    ds = {"id": 7, "metrics": [{"name": "m", "expression": "COUNT(*)"}], "columns": [{"name": "a"}]}
    assert reason in (chart_query({**form_data, "datasource": "7__table"}, ds).unsupported or "")


def test_adhoc_metrics_and_filters_translate():
    ds = {"id": 7, "metrics": [], "columns": [{"name": "d", "is_dttm": True}, {"name": "a"}], "main_dttm_col": "d"}
    q = chart_query({"viz_type": "echarts_timeseries_bar", "datasource": "7__table", "x_axis": "d", "time_grain_sqla": "P1M",
                     "groupby": ["a"], "row_limit": 500,
                     "metrics": [{"expressionType": "SIMPLE", "aggregate": "COUNT_DISTINCT", "column": {"column_name": "id"},
                                  "label": "ids"},
                                 {"expressionType": "SQL", "sqlExpression": "SUM(x) / COUNT(*)", "label": "ratio"}],
                     "adhoc_filters": [{"expressionType": "SIMPLE", "clause": "WHERE", "subject": "a", "operator": "IN",
                                        "comparator": ["x", "y"]},
                                       {"expressionType": "SIMPLE", "clause": "WHERE", "subject": "d",
                                        "operator": "TEMPORAL_RANGE", "comparator": "No filter"},
                                       {"expressionType": "SQL", "clause": "WHERE", "sqlExpression": "b > 2"}]}, ds)
    assert q.unsupported is None
    assert [(m.label, m.expression) for m in q.metrics] == [("ids", "COUNT(DISTINCT id)"), ("ratio", "SUM(x) / COUNT(*)")]
    assert (q.time_column, q.time_grain, q.dimensions, q.row_limit) == ("d", "month", ["a"], 500)
    assert [(f.column, f.op, f.value) for f in q.filters] == [("a", "IN", ["x", "y"])] and q.sql_filters == ["b > 2"]
