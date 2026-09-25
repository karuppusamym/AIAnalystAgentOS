"""ChartSpec -> Superset viz_type/params/query_context mapping, and position_json layout."""
from __future__ import annotations

import json

import pytest

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef
from analystos.publishing.layout import build_position_json, layout_rows, parse_position_json
from analystos.publishing.superset_charts import build_chart

DS = DatasetDef(
    name="incidents",
    sql="SELECT * FROM src_publishtest.incidents",
    columns=[{"name": c} for c in ["opened_at", "priority", "assignment_group", "resolution_hours", "made_sla"]],
    time_column="opened_at",
)
METRICS = {
    "incident_count": MetricDef(name="incident_count", display_name="Incidents", definition="", sql_expression="COUNT(*)"),
    "sla_rate": MetricDef(name="sla_rate", display_name="SLA", definition="", sql_expression="AVG(x)", format="percent"),
    "avg_hours": MetricDef(name="avg_hours", display_name="Hours", definition="", sql_expression="AVG(h)", format="hours",
                           source_columns=["resolution_hours"]),
}


def chart(chart_type: str, **kw) -> ChartSpec:
    return ChartSpec(key=f"c_{chart_type}", title=chart_type, chart_type=chart_type, intent="x", dataset="incidents", **kw)


def build(c: ChartSpec):
    viz, params, qc = build_chart(c, dataset_id=7, dataset=DS, metrics=METRICS, aos={"key": c.key, "workspace": "ws"})
    json.dumps(params), json.dumps(qc)  # must be JSON serialisable
    assert params["viz_type"] == viz and params["datasource"] == "7__table"
    assert qc["datasource"] == {"id": 7, "type": "table"} and qc["result_type"] == "full"
    assert qc["form_data"]["viz_type"] == viz and "aos_key" not in qc["form_data"]
    assert params["aos_key"] == c.key
    assert len(qc["queries"]) == 1
    return viz, params, qc["queries"][0]


def test_kpi():
    viz, p, q = build(chart("kpi", metric="sla_rate"))
    assert viz == "big_number_total" and p["metric"] == "sla_rate" and p["y_axis_format"] == ".1%"
    assert q["metrics"] == ["sla_rate"] and q["columns"] == []


@pytest.mark.parametrize(("grain", "iso"), [("day", "P1D"), ("week", "P1W"), ("month", "P1M"), (None, "P1D")])
def test_line_time_axis(grain, iso):
    viz, p, q = build(chart("line", metric="incident_count", series="priority", time_grain=grain))
    assert viz == "echarts_timeseries_line"
    assert p["x_axis"] == "opened_at" and p["time_grain_sqla"] == iso and p["groupby"] == ["priority"]
    axis = q["columns"][0]
    assert axis == {"timeGrain": iso, "columnType": "BASE_AXIS", "sqlExpression": "opened_at", "label": "opened_at",
                    "expressionType": "SQL"}
    assert q["columns"][1:] == ["priority"] and q["series_columns"] == ["priority"]
    assert q["filters"] == [{"col": "opened_at", "op": "TEMPORAL_RANGE", "val": "No filter"}]


def test_bar_categorical_dimension():
    viz, p, q = build(chart("bar", metric="avg_hours", dimension="priority", limit=20))
    assert viz == "echarts_timeseries_bar" and p["x_axis"] == "priority" and "time_grain_sqla" not in p
    assert p["x_axis_sort"] == "avg_hours" and p["x_axis_sort_asc"] is False and "stack" not in p
    assert q["columns"] == ["priority"] and q["orderby"] == [["avg_hours", False]] and q["row_limit"] == 20
    assert p["y_axis_format"] == ",.1f"


def test_bar_without_dimension_uses_time_axis():
    viz, p, q = build(chart("bar", metric="incident_count", time_grain="month"))
    assert viz == "echarts_timeseries_bar" and p["x_axis"] == "opened_at" and p["time_grain_sqla"] == "P1M"
    assert q["columns"][0]["columnType"] == "BASE_AXIS"


def test_stacked_bar():
    viz, p, q = build(chart("stacked_bar", metric="incident_count", dimension="assignment_group", series="priority"))
    assert viz == "echarts_timeseries_bar" and p["stack"] == "Stack" and p["groupby"] == ["priority"]
    assert q["columns"] == ["assignment_group", "priority"]


def test_histogram_uses_dimension_or_metric_source_column():
    viz, p, q = build(chart("histogram", dimension="resolution_hours"))
    assert viz == "histogram_v2" and p["column"] == "resolution_hours" and p["bins"] == 10
    assert q["metrics"] == [] and q["columns"] == ["resolution_hours"]
    assert q["post_processing"][0]["operation"] == "histogram"
    _, p2, _ = build(chart("histogram", metric="avg_hours"))
    assert p2["column"] == "resolution_hours"


def test_scatter_time_and_bubble():
    viz, p, _ = build(chart("scatter", metric="incident_count"))
    assert viz == "echarts_timeseries_scatter" and p["x_axis"] == "opened_at"
    viz, p, q = build(chart("scatter", metrics=["incident_count", "avg_hours", "sla_rate"], dimension="assignment_group"))
    assert viz == "bubble_v2"
    assert (p["entity"], p["x"], p["y"], p["size"]) == ("assignment_group", "incident_count", "avg_hours", "sla_rate")
    assert q["metrics"] == ["incident_count", "avg_hours", "sla_rate"] and q["columns"] == ["assignment_group"]


def test_heatmap():
    viz, p, q = build(chart("heatmap", metric="incident_count", dimension="assignment_group", series="priority"))
    assert viz == "heatmap_v2" and p["x_axis"] == "assignment_group" and p["groupby"] == "priority"
    assert p["metric"] == "incident_count" and q["columns"] == ["assignment_group", "priority"]


def test_table_aggregate_and_raw():
    viz, p, q = build(chart("table", metrics=["incident_count", "sla_rate"], dimension="assignment_group"))
    assert viz == "table" and p["query_mode"] == "aggregate"
    assert p["groupby"] == ["assignment_group"] and p["metrics"] == ["incident_count", "sla_rate"]
    assert q["orderby"] == [["incident_count", False]]
    _, p2, q2 = build(chart("table"))
    assert p2["query_mode"] == "raw" and p2["all_columns"] == [c["name"] for c in DS.columns]
    assert q2["metrics"] == [] and q2["columns"] == p2["all_columns"]


def test_pie_and_treemap():
    viz, p, q = build(chart("pie", metric="incident_count", dimension="priority"))
    assert viz == "pie" and p["groupby"] == ["priority"] and p["metric"] == "incident_count"
    assert q["columns"] == ["priority"]
    viz, p, q = build(chart("treemap", metric="incident_count", dimension="assignment_group", series="priority"))
    assert viz == "treemap_v2" and p["groupby"] == ["assignment_group", "priority"]


def test_chart_filters_become_adhoc_sql_and_query_where():
    _, p, q = build(chart("kpi", metric="incident_count", filters=["priority = '1'", "made_sla"]))
    assert [f["sqlExpression"] for f in p["adhoc_filters"]] == ["priority = '1'", "made_sla"]
    assert all(f["expressionType"] == "SQL" and f["clause"] == "WHERE" for f in p["adhoc_filters"])
    assert q["extras"]["where"] == "(priority = '1') AND (made_sla)"


# -- layout ----------------------------------------------------------------------------------------

def _check_position(pos: dict, chart_ids: dict[str, int]) -> None:
    assert pos["ROOT_ID"]["children"] == ["GRID_ID"]
    rows = pos["GRID_ID"]["children"]
    seen: list[int] = []
    for rid in rows:
        row = pos[rid]
        assert row["type"] == "ROW" and row["parents"] == ["ROOT_ID", "GRID_ID"]
        assert sum(pos[c]["meta"]["width"] for c in row["children"]) <= 12
        for cid in row["children"]:
            node = pos[cid]
            assert node["parents"] == ["ROOT_ID", "GRID_ID", rid] and node["id"] == cid
            if node["type"] == "CHART":
                seen.append(node["meta"]["chartId"])
                assert node["meta"]["height"] > 0
    assert sorted(seen) == sorted(chart_ids.values()), "every chart exactly once"
    referenced = {c for n in pos.values() if isinstance(n, dict) for c in n.get("children", [])}
    nodes = {k for k, v in pos.items() if isinstance(v, dict) and v.get("type") in {"ROW", "CHART", "MARKDOWN"}}
    assert nodes == referenced - {"GRID_ID"}, "no orphan nodes"


def test_position_json_from_explicit_layout_with_markdown():
    d = DashboardSpec(key="exec", title="Exec", audience="executive", charts=["a", "b", "c", "d"],
                      summary_markdown="# Summary",
                      layout=[{"chart": "a", "row": 0, "col": 0, "width": 3, "height": 3},
                              {"chart": "b", "row": 0, "col": 1, "width": 9},
                              {"chart": "c", "row": 1, "col": 0, "width": 6, "height": 60},
                              {"chart": "d", "row": 1, "col": 1, "width": 6}])
    ids = {"a": 1, "b": 2, "c": 3, "d": 4}
    pos = build_position_json(d, ids, chart_types={"a": "kpi"}, chart_titles={"a": "KPI A"})
    _check_position(pos, ids)
    assert pos["HEADER_ID"]["meta"]["text"] == "Exec"
    assert pos["ROW-1"]["children"] == ["MARKDOWN-summary"] and pos["MARKDOWN-summary"]["meta"]["code"] == "# Summary"
    assert [pos[c]["meta"]["chartId"] for c in pos["ROW-2"]["children"]] == [1, 2]
    assert pos["CHART-a"]["meta"]["height"] == 36 and pos["CHART-c"]["meta"]["height"] == 60
    assert pos["CHART-a"]["meta"]["sliceNameOverride"] == "KPI A"
    assert [x["chart_id"] for x in parse_position_json(pos)] == [1, 2, 3, 4]


def test_position_json_overfull_rows_spill_and_missing_charts_are_appended():
    d = DashboardSpec(key="ops", title="Ops", audience="operational", charts=["a", "b", "c", "d", "e"],
                      layout=[{"chart": "a", "row": 0, "col": 0, "width": 8},
                              {"chart": "b", "row": 0, "col": 1, "width": 8},
                              {"chart": "c", "row": 0, "col": 2, "width": 20}])
    ids = {k: i for i, k in enumerate("abcde", start=10)}
    pos = build_position_json(d, ids, chart_types={"d": "kpi", "e": "table"})
    _check_position(pos, ids)
    rows = layout_rows(d, {"d": "kpi", "e": "table"})
    assert [[k for k, *_ in r] for r in rows] == [["a"], ["b"], ["c"], ["d"], ["e"]]
    assert rows[2][0][1] == 12


def test_position_json_auto_layout_when_none_given():
    d = DashboardSpec(key="x", title="X", audience="executive", charts=["k1", "k2", "k3", "k4", "l", "t"])
    types = {"k1": "kpi", "k2": "kpi", "k3": "kpi", "k4": "kpi", "l": "line", "t": "table"}
    ids = {k: i for i, k in enumerate(types)}
    pos = build_position_json(d, ids, chart_types=types)
    _check_position(pos, ids)
    rows = layout_rows(d, types)
    assert [len(r) for r in rows] == [4, 1, 1]
