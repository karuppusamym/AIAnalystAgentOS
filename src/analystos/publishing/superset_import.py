"""Superset chart ``form_data`` -> ``BIChartQuery`` (existing-dashboard mode, v1 §35, BI-011).

The reverse of ``superset_charts``: read what a chart someone else built aggregates, over which columns,
with which filters, so the platform can re-execute it through the gateway. Anything whose numbers the
platform cannot reproduce exactly (relative time ranges, rolling windows, time comparison, contribution
mode, custom SQL columns, raw-record tables, histograms) is reported as ``unsupported`` with the reason,
never approximated.
"""
from __future__ import annotations

import json
import re
from typing import Any

from analystos.contracts.bi import BIChartQuery, BIFilter, BIMetricRef

GRAINS = {"P1D": "day", "P1W": "week", "P1M": "month", "P3M": "quarter"}
_AGG = {"COUNT", "SUM", "AVG", "MIN", "MAX", "COUNT_DISTINCT"}
_OPS = {"==", "!=", "IN", "NOT IN", ">", ">=", "<", "<=", "IS NULL", "IS NOT NULL", "LIKE"}
_NO_TIME_RANGE = (None, "", "No filter")
_AGGREGATE_VIZ = {
    "big_number_total", "big_number", "pie", "table", "echarts_timeseries", "echarts_timeseries_line",
    "echarts_timeseries_bar", "echarts_timeseries_scatter", "echarts_timeseries_smooth", "echarts_timeseries_step",
    "echarts_area", "bubble_v2", "heatmap_v2", "treemap_v2", "funnel", "dist_bar", "bar", "line",
}
_VALUE_CHANGING = ("rolling_type", "time_compare", "contributionMode", "contribution_mode", "resample_rule")
_SIMPLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Unsupported(Exception):
    pass


def _quote(column: str) -> str:
    return column if _SIMPLE_NAME.match(column) else '"' + column.replace('"', '""') + '"'


def is_certified(metric: dict[str, Any]) -> bool:
    try:
        extra = json.loads(metric.get("extra") or "{}") if isinstance(metric.get("extra"), str) else metric.get("extra") or {}
    except ValueError:
        return False
    return bool((extra or {}).get("certification"))


def _metric(m: Any, saved: dict[str, dict[str, Any]]) -> BIMetricRef:
    if isinstance(m, str):
        row = saved.get(m)
        return BIMetricRef(label=m, expression=(row or {}).get("expression"), saved=True,
                           certified=bool(row and (row.get("certified") or is_certified(row))))
    if not isinstance(m, dict):
        raise _Unsupported(f"metric {m!r} is neither a saved metric nor an ad-hoc metric")
    if m.get("expressionType") == "SQL":
        expr = str(m.get("sqlExpression") or "").strip()
        if not expr:
            raise _Unsupported("an ad-hoc SQL metric has no expression")
        return BIMetricRef(label=str(m.get("label") or expr), expression=expr)
    agg = str(m.get("aggregate") or "").upper()
    col = (m.get("column") or {}).get("column_name") if isinstance(m.get("column"), dict) else m.get("column")
    if agg not in _AGG or not col:
        raise _Unsupported(f"ad-hoc metric {m.get('label')!r} uses aggregate {agg or '?'} on {col or '?'}")
    expr = f"COUNT(DISTINCT {_quote(col)})" if agg == "COUNT_DISTINCT" else f"{agg}({_quote(col)})"
    return BIMetricRef(label=str(m.get("label") or f"{agg}({col})"), expression=expr)


def _column(c: Any) -> str:
    if isinstance(c, str) and c:
        return c
    if isinstance(c, dict) and c.get("expressionType") != "SQL" and c.get("column_name"):
        return str(c["column_name"])
    if isinstance(c, dict) and c.get("expressionType") == "SQL" and _SIMPLE_NAME.match(str(c.get("sqlExpression") or "")):
        return str(c["sqlExpression"])  # a BASE_AXIS over a plain column (what AnalystOS itself publishes)
    raise _Unsupported(f"column {c!r} is a custom SQL expression")


def _filters(adhoc: list[dict[str, Any]]) -> tuple[list[BIFilter], list[str]]:
    simple: list[BIFilter] = []
    sql: list[str] = []
    for f in adhoc or []:
        clause = str(f.get("clause") or "WHERE").upper()
        if clause != "WHERE":
            raise _Unsupported(f"a {clause} filter is not reproducible")
        if f.get("expressionType") == "SQL":
            text = str(f.get("sqlExpression") or "").strip()
            if text:
                sql.append(text)
            continue
        op = str(f.get("operator") or "").upper()
        subject = f.get("subject")
        if op == "TEMPORAL_RANGE":
            if f.get("comparator") in _NO_TIME_RANGE:
                continue
            raise _Unsupported(f"time range {f.get('comparator')!r} is relative to when the chart runs")
        if op not in _OPS or not isinstance(subject, str) or not subject:
            raise _Unsupported(f"filter {op or '?'} on {subject!r} is not reproducible")
        value = f.get("comparator")
        if op in ("IN", "NOT IN") and not isinstance(value, list):
            value = [value]
        simple.append(BIFilter(column=subject, op=op, value=value))  # type: ignore[arg-type]
    return simple, sql


def chart_query(form_data: dict[str, Any], dataset: dict[str, Any] | None) -> BIChartQuery:
    """Translate one chart. ``dataset`` is the inspected dataset ({id, main_dttm_col, columns, metrics})."""
    fd = form_data or {}
    ds_id = str((dataset or {}).get("id") or str(fd.get("datasource") or "").split("__", 1)[0])
    try:
        return _translate(fd, dataset or {}, ds_id)
    except _Unsupported as exc:
        return BIChartQuery(dataset_id=ds_id, unsupported=str(exc))


def _translate(fd: dict[str, Any], dataset: dict[str, Any], ds_id: str) -> BIChartQuery:
    viz = str(fd.get("viz_type") or "")
    if not dataset:
        raise _Unsupported("the chart's dataset is not visible to this workspace")
    if viz not in _AGGREGATE_VIZ:
        raise _Unsupported(f"a {viz or 'chart'} does not show an aggregate the platform can re-execute")
    if viz == "table" and fd.get("query_mode") == "raw":
        raise _Unsupported("a raw-records table has no aggregate to verify")
    for key in _VALUE_CHANGING:
        if fd.get(key) not in (None, "", "None", [], "null"):
            raise _Unsupported(f"{key} changes the values after the query")
    if fd.get("time_range") not in _NO_TIME_RANGE:
        raise _Unsupported(f"time range {fd.get('time_range')!r} is relative to when the chart runs")
    saved = {m.get("name") or m.get("metric_name"): m for m in dataset.get("metrics") or []}
    raw_metrics = list(fd.get("metrics") or [])
    for key in ("metric", "x", "y", "size"):
        if fd.get(key):
            raw_metrics.append(fd[key])
    metrics: list[BIMetricRef] = []
    for m in raw_metrics:
        ref = _metric(m, saved)
        if ref.label not in {x.label for x in metrics}:
            metrics.append(ref)
    if not metrics:
        raise _Unsupported("the chart has no metric")
    missing = [m.label for m in metrics if m.expression is None]
    if missing:
        raise _Unsupported(f"saved metric(s) {', '.join(missing)} are not defined on the dataset")

    temporal = {c.get("name") or c.get("column_name") for c in dataset.get("columns") or [] if c.get("is_dttm")}
    temporal |= {x for x in (dataset.get("main_dttm_col"), fd.get("granularity_sqla")) if x}
    dims: list[str] = []
    time_column: str | None = None
    x_axis = fd.get("x_axis")
    if x_axis:
        x = _column(x_axis)
        if x in temporal and (fd.get("time_grain_sqla") or (isinstance(x_axis, dict) and x_axis.get("timeGrain"))):
            time_column = x
        else:
            dims.append(x)
    elif viz in ("big_number", "line", "echarts_timeseries", "echarts_timeseries_line", "echarts_timeseries_bar",
                 "echarts_area") and fd.get("granularity_sqla"):
        time_column = str(fd["granularity_sqla"])
    for key in ("entity", "series"):
        if fd.get(key):
            dims.append(_column(fd[key]))
    groupby = fd.get("groupby") or []
    for g in groupby if isinstance(groupby, list) else [groupby]:
        dims.append(_column(g))
    for g in fd.get("columns") or [] if viz in ("dist_bar", "bar") else []:
        dims.append(_column(g))
    grain = None
    if time_column:
        iso = fd.get("time_grain_sqla") or (x_axis.get("timeGrain") if isinstance(x_axis, dict) else None) or "P1D"
        grain = GRAINS.get(str(iso))
        if grain is None:
            raise _Unsupported(f"time grain {iso!r} is not reproducible")
    filters, sql_filters = _filters(fd.get("adhoc_filters") or [])
    limit = fd.get("row_limit")
    return BIChartQuery(dataset_id=ds_id, metrics=metrics, dimensions=list(dict.fromkeys(dims)), time_column=time_column,
                        time_grain=grain, filters=filters, sql_filters=sql_filters,  # type: ignore[arg-type]
                        row_limit=int(limit) if str(limit or "").isdigit() else None)
