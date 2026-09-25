"""ChartSpec -> Superset 4.x ``viz_type`` + ``params`` (form_data) + ``query_context``.

``params`` is what the Explore UI / dashboard renders from. ``query_context`` is what the server
uses for ``GET /api/v1/chart/{id}/data/`` (reports, alerts, cache warm-up, and our render check);
without it a chart has to be opened and saved in the UI once before those paths work.

Metrics are referenced by their saved ``metric_name`` (registered on the dataset beforehand), so
Superset's own metric definition is the single source of truth for the SQL.
"""
from __future__ import annotations

from typing import Any

from analystos.contracts.bi import ChartSpec, DatasetDef, MetricDef
from analystos.core.errors import InvalidInput
from analystos.publishing.preview import _histogram_column, chart_metric_names

TIME_GRAINS = {"day": "P1D", "week": "P1W", "month": "P1M"}
D3_FORMATS = {"percent": ".1%", "hours": ",.1f", "currency": "$,.2f"}

# ChartSpec.chart_type -> Superset viz_type (scatter switches to bubble_v2 with a dimension + 2 metrics).
VIZ_TYPES: dict[str, str] = {
    "kpi": "big_number_total",
    "line": "echarts_timeseries_line",
    "bar": "echarts_timeseries_bar",
    "stacked_bar": "echarts_timeseries_bar",
    "histogram": "histogram_v2",
    "scatter": "echarts_timeseries_scatter",
    "heatmap": "heatmap_v2",
    "table": "table",
    "pie": "pie",
    "treemap": "treemap_v2",
}


def metric_d3format(metric: MetricDef | None) -> str | None:
    return D3_FORMATS.get(metric.format) if metric else None


def _y_format(metric: MetricDef | None) -> str:
    return metric_d3format(metric) or "SMART_NUMBER"


def _adhoc_filters(chart: ChartSpec) -> list[dict[str, Any]]:
    return [
        {
            "expressionType": "SQL",
            "sqlExpression": f,
            "clause": "WHERE",
            "subject": None,
            "operator": None,
            "comparator": None,
            "isExtra": False,
            "isNew": False,
            "filterOptionName": f"aos_filter_{i}",
        }
        for i, f in enumerate(chart.filters)
    ]


def _where(chart: ChartSpec) -> str:
    return " AND ".join(f"({f})" for f in chart.filters)


def _time_axis(column: str, grain: str) -> dict[str, Any]:
    return {"timeGrain": grain, "columnType": "BASE_AXIS", "sqlExpression": column, "label": column, "expressionType": "SQL"}


def _query(
    *,
    columns: list[Any],
    metrics: list[str],
    chart: ChartSpec,
    row_limit: int,
    orderby: list[list[Any]] | None = None,
    time_column: str | None = None,
    time_grain: str | None = None,
    series_columns: list[str] | None = None,
    post_processing: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    extras: dict[str, Any] = {"having": "", "where": _where(chart)}
    if time_grain:
        extras["time_grain_sqla"] = time_grain
    filters = [{"col": time_column, "op": "TEMPORAL_RANGE", "val": "No filter"}] if time_column else []
    q: dict[str, Any] = {
        "filters": filters,
        "extras": extras,
        "applied_time_extras": {},
        "columns": columns,
        "metrics": metrics,
        "orderby": orderby or [],
        "annotation_layers": [],
        "row_limit": row_limit,
        "series_limit": 0,
        "order_desc": True,
        "url_params": {},
        "custom_params": {},
        "custom_form_data": {},
        "post_processing": post_processing or [],
    }
    if time_column:
        q["time_range"] = "No filter"
    if series_columns:
        q["series_columns"] = series_columns
    return q


def build_chart(
    chart: ChartSpec,
    *,
    dataset_id: int,
    dataset: DatasetDef,
    metrics: dict[str, MetricDef],
    aos: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Returns (viz_type, params, query_context). ``aos`` keys are stamped into params for reconcile."""
    mnames = chart_metric_names(chart)
    primary = mnames[0] if mnames else ""  # validate_bundle guarantees one for metric-bearing types
    pmetric = metrics.get(primary)
    grain = TIME_GRAINS.get(chart.time_grain or "", "P1D")
    time_col = dataset.time_column
    series = [chart.series] if chart.series else []
    t = chart.chart_type
    viz = VIZ_TYPES[t]
    limit = chart.limit

    params: dict[str, Any] = {
        "datasource": f"{dataset_id}__table",
        "adhoc_filters": _adhoc_filters(chart),
        "extra_form_data": {},
    }

    if t == "kpi":
        params.update(
            metric=primary,
            header_font_size=0.4,
            subheader_font_size=0.15,
            y_axis_format=_y_format(pmetric),
            time_format="smart_date",
            subheader=chart.description[:120] if chart.description else "",
        )
        query = _query(columns=[], metrics=[primary], row_limit=limit or 1, chart=chart)

    elif t in {"line", "bar", "stacked_bar"} or (t == "scatter" and not (chart.dimension and len(mnames) >= 2)):
        categorical = t in {"bar", "stacked_bar", "scatter"} and bool(chart.dimension)
        if not categorical and not time_col:
            raise InvalidInput(f"chart {chart.key!r}: {t} needs a time column or a dimension")
        x_axis = chart.dimension if categorical else time_col
        row_limit = limit or (100 if categorical else 10000)
        params.update(
            x_axis=x_axis,
            metrics=mnames,
            groupby=series,
            row_limit=row_limit,
            truncate_metric=True,
            show_legend=True,
            legendType="scroll",
            legendOrientation="top",
            rich_tooltip=True,
            y_axis_format=_y_format(pmetric),
            x_axis_time_format="smart_date",
            comparison_type="values",
            order_desc=True,
        )
        if not categorical:
            params.update(time_grain_sqla=grain, time_range="No filter", granularity_sqla=time_col)
        if t in {"bar", "stacked_bar"}:
            params.update(orientation="vertical", show_value=not series and t == "bar")
            if categorical and not series:
                params.update(x_axis_sort=primary, x_axis_sort_asc=False)
        if t == "stacked_bar":
            params["stack"] = "Stack"
        if t == "line":
            params.update(seriesType="line", markerEnabled=False)
        if t == "scatter":
            params.update(markerEnabled=True, markerSize=6)
        x_col: Any = _time_axis(str(time_col), grain) if not categorical else x_axis
        query = _query(
            columns=[x_col, *series],
            metrics=mnames,
            orderby=[[primary, False]] if categorical else [],
            row_limit=row_limit,
            time_column=None if categorical else time_col,
            time_grain=None if categorical else grain,
            series_columns=series,
            chart=chart,
        )

    elif t == "scatter":  # dimension + >= 2 metrics -> bubble_v2
        viz = "bubble_v2"
        x, y = mnames[0], mnames[1]
        size = mnames[2] if len(mnames) > 2 else mnames[0]
        row_limit = limit or 1000
        params.update(
            entity=chart.dimension,
            series=chart.series,
            x=x,
            y=y,
            size=size,
            max_bubble_size="25",
            row_limit=row_limit,
            show_legend=True,
            x_axis_format=_y_format(metrics.get(x)),
            y_axis_format=_y_format(metrics.get(y)),
            order_desc=True,
        )
        cols = [chart.dimension, *series]
        query = _query(
            columns=cols, metrics=list(dict.fromkeys([x, y, size])), orderby=[[size, False]], row_limit=row_limit, chart=chart
        )

    elif t == "histogram":
        column = _histogram_column(chart, metrics)
        if not column:
            raise InvalidInput(f"chart {chart.key!r}: histogram needs a numeric column")
        row_limit = limit or 10000
        params.update(column=column, groupby=series, bins=10, normalize=False, cumulative=False, row_limit=row_limit,
                      x_axis_title=column, y_axis_title="Count", show_legend=bool(series))
        query = _query(
            columns=[column, *series],
            metrics=[],
            row_limit=row_limit,
            post_processing=[
                {
                    "operation": "histogram",
                    "options": {"column": column, "groupby": series, "bins": 10, "cumulative": False, "normalize": False},
                }
            ],
            chart=chart,
        )

    elif t == "heatmap":
        y_col = chart.series or time_col
        x_col = chart.dimension
        row_limit = limit or 10000
        params.update(
            x_axis=x_col,
            groupby=y_col,
            metric=primary,
            row_limit=row_limit,
            sort_x_axis="alpha_asc",
            sort_y_axis="alpha_asc",
            normalize_across="heatmap",
            legend_type="continuous",
            linear_color_scheme="superset_seq_1",
            y_axis_format=_y_format(pmetric),
            x_axis_time_format="smart_date",
            show_legend=True,
            show_percentage=True,
            value_bounds=[None, None],
        )
        query = _query(columns=[str(x_col), str(y_col)], metrics=[primary], row_limit=row_limit, chart=chart)

    elif t == "table":
        groupby = [c for c in [chart.dimension, chart.series] if c]
        row_limit = limit or 1000
        if mnames or groupby:
            params.update(query_mode="aggregate", groupby=groupby, metrics=mnames, all_columns=[], percent_metrics=[],
                          order_desc=True, row_limit=row_limit, server_page_length=10, show_cell_bars=True,
                          table_timestamp_format="smart_date", include_search=False)
            query = _query(columns=groupby, metrics=mnames, orderby=[[mnames[0], False]] if mnames else [],
                           row_limit=row_limit, chart=chart)
        else:
            cols = [c["name"] for c in dataset.columns if c.get("name")]
            params.update(query_mode="raw", all_columns=cols, groupby=[], metrics=[], order_by_cols=[],
                          row_limit=row_limit, server_page_length=10, table_timestamp_format="smart_date")
            query = _query(columns=cols, metrics=[], row_limit=row_limit, chart=chart)

    elif t == "pie":
        row_limit = limit or 25
        params.update(groupby=[chart.dimension], metric=primary, row_limit=row_limit, sort_by_metric=True,
                      show_labels=True, labels_outside=True, label_type="key_percent", number_format=_y_format(pmetric),
                      donut=False, show_legend=True, legendType="scroll", legendOrientation="top",
                      color_scheme="supersetColors", outerRadius=70, innerRadius=30)
        query = _query(columns=[str(chart.dimension)], metrics=[primary], orderby=[[primary, False]], row_limit=row_limit,
                       chart=chart)

    elif t == "treemap":
        groupby = [c for c in [chart.dimension, chart.series] if c]
        row_limit = limit or 1000
        params.update(groupby=groupby, metric=primary, row_limit=row_limit, number_format=_y_format(pmetric),
                      label_type="key_value", show_labels=True, show_upper_labels=True, color_scheme="supersetColors")
        query = _query(columns=groupby, metrics=[primary], orderby=[[primary, False]], row_limit=row_limit, chart=chart)

    else:  # pragma: no cover - ChartType is a closed Literal
        raise InvalidInput(f"unsupported chart type {t!r}")

    params["viz_type"] = viz
    if aos:
        params.update({f"aos_{k}": v for k, v in aos.items()})
    form_data = {k: v for k, v in params.items() if not k.startswith("aos_")}
    query_context = {
        "datasource": {"id": dataset_id, "type": "table"},
        "force": False,
        "queries": [query],
        "form_data": form_data,
        "result_format": "json",
        "result_type": "full",
    }
    return viz, params, query_context
