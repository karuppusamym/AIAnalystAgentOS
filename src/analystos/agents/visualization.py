"""Visualization Agent (§13.14, §33, §34): chart specs from verified findings + KPI set, previews
computed through the gateway, executive and operational dashboards laid out on a 12-column grid."""
from __future__ import annotations

from typing import Any

import numpy as np
import sqlglot
from sqlalchemy import select

from analystos.agents.sql_agent import dataset_def, derivation_alias
from analystos.artifacts.registry import link, save_artifact
from analystos.contracts.analysis import AnalysisSpec, Derivation
from analystos.contracts.bi import ChartSpec, DashboardSpec, MetricDef
from analystos.core.errors import AnalystOSError
from analystos.db.base import session_scope
from analystos.db.models import Artifact, Hypothesis, Insight
from analystos.runtime.context import RunContext

JEV_ALTERNATIVES = {"comparison": ["bar", "treemap", "pie", "table"], "distribution": ["histogram", "bar"],
                    "trend": ["line", "bar"], "part_to_whole": ["treemap", "stacked_bar", "pie", "bar"]}


def _q(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def preview_sql(chart: ChartSpec, ds_sql: str, metrics: dict[str, MetricDef]) -> str:
    src = f"({ds_sql}) d"
    where = (" WHERE " + " AND ".join(chart.filters)) if chart.filters else ""
    m = metrics.get(chart.metric or "") if chart.metric else None
    if chart.chart_type == "kpi" and m:
        return f"SELECT {m.sql_expression} AS value FROM {src}{where}"
    if chart.chart_type == "line" and m and chart.dimension:
        return f"SELECT {_q(chart.dimension)} AS x, {m.sql_expression} AS y FROM {src}{where} GROUP BY 1 ORDER BY 1"
    if chart.chart_type == "histogram" and chart.dimension:
        return f"SELECT {_q(chart.dimension)} AS v FROM {src}{where}{' AND' if where else ' WHERE'} {_q(chart.dimension)} IS NOT NULL"
    if chart.chart_type == "heatmap" and chart.dimension and chart.series:
        return f"SELECT {_q(chart.dimension)} AS x, {_q(chart.series)} AS y, COUNT(*) AS v FROM {src}{where} GROUP BY 1, 2 ORDER BY 1, 2"
    if chart.chart_type == "table" and chart.dimension:
        exprs = ", ".join(f"{metrics[n].sql_expression} AS {_q(n)}" for n in chart.metrics if n in metrics)
        return f"SELECT {_q(chart.dimension)}, {exprs} FROM {src}{where} GROUP BY 1 ORDER BY 2 DESC LIMIT {chart.limit or 20}"
    if m and chart.dimension:
        order = "1" if chart.dimension.endswith(("_bucket", "_hour", "_dow")) else "2 DESC"
        return f"SELECT {_q(chart.dimension)} AS x, {m.sql_expression} AS y FROM {src}{where} GROUP BY 1 ORDER BY {order} LIMIT {chart.limit or 15}"
    raise ValueError(f"cannot build preview for {chart.key}")


def _metric_for_outcome(spec: AnalysisSpec, metrics: dict[str, MetricDef]) -> str | None:
    if spec.outcome is None or spec.method in ("pareto", "trend"):
        return "record_count" if "record_count" in metrics else None
    alias = derivation_alias(spec.outcome)
    for cand in (f"{alias}_rate", f"median_{alias}", f"avg_{alias}"):
        if cand in metrics:
            return cand
    return None


def _choose(ctx: RunContext, intent: str, dim_type: str | None, cardinality: int, title: str) -> tuple[str, str]:
    from analystos.skills.viz import choose_chart

    chart_type, rationale = choose_chart(intent, dim_type, cardinality, 1)
    options = {t: {"bar": "bar chart comparing categories", "treemap": "treemap of part-to-whole shares",
                   "pie": "pie chart of shares (few categories)", "table": "detail table", "histogram": "histogram of a distribution",
                   "line": "line chart over time", "stacked_bar": "stacked bars of composition"}[t]
               for t in JEV_ALTERNATIVES.get(intent, []) if not (t == "pie" and cardinality > 5)}
    if chart_type in options and len(options) > 1:
        v = ctx.jev.choose("chart_selection", {"chart_title": title, "intent": intent, "categories": str(cardinality)},
                           "Which chart type communicates `chart_title` best to a business audience?", options, ctx=ctx.call_ctx())
        if v and v.value != chart_type and v.probabilities.get(v.value, 0) >= 0.6:
            return v.value, f"{rationale}; overridden by JEV ({v.value} p={v.probabilities.get(v.value):.2f})"
        if v:
            rationale += f"; JEV agrees ({chart_type} p={v.probabilities.get(chart_type, 0):.2f})"
    return chart_type, rationale


def design(ctx: RunContext) -> dict:
    from analystos.skills.dashboards import build_layout, choose_native_filters

    ds, content = dataset_def(ctx.run.id)
    with session_scope() as s:
        metrics = {a.name: MetricDef.model_validate(a.content) for a in s.scalars(
            select(Artifact).where(Artifact.run_id == ctx.run.id, Artifact.type == "metric"))}
        verified = [(i.code, i.title, AnalysisSpec.model_validate(h.spec)) for i, h in s.execute(
            select(Insight, Hypothesis).join(Hypothesis, Insight.hypothesis_id == Hypothesis.id)
            .where(Insight.run_id == ctx.run.id, Insight.status == "verified").order_by(Insight.confidence.desc())).all()]
    cols = {c["name"]: c for c in ds.columns}
    charts: list[ChartSpec] = []
    kpi_order = ["record_count"] + [n for n in metrics if n.endswith("_rate")] + [n for n in metrics if n.startswith("median_")] + \
        [n for n in metrics if n.startswith("avg_")]
    for name in list(dict.fromkeys(kpi_order))[:4]:
        charts.append(ChartSpec(key=f"kpi_{name}", title=metrics[name].display_name, chart_type="kpi", intent="kpi",
                                dataset=ds.name, metric=name, rationale="KPI card (§34)"))
    if ds.time_column:
        charts.append(ChartSpec(key="trend_volume", title="Volume by month", chart_type="line", intent="trend", dataset=ds.name,
                                metric="record_count", dimension=ds.time_column, time_grain="month", rationale="trend -> line"))
        rate = next((n for n in metrics if n.endswith("_rate")), None)
        if rate:
            charts.append(ChartSpec(key=f"trend_{rate}", title=f"{metrics[rate].display_name} by month", chart_type="line",
                                    intent="trend", dataset=ds.name, metric=rate, dimension=ds.time_column, time_grain="month",
                                    rationale="trend -> line"))
    for code, title, spec in verified:
        metric = _metric_for_outcome(spec, metrics)
        seg = spec.segment
        dim = derivation_alias(seg) if seg is not None else None
        if spec.method == "trend" or not metric or not dim or dim not in cols:
            continue
        # Filters on dataset columns only (raw columns keep their names in the dataset).
        filters = [f"{_q(f.column)} {f.op} {repr(f.value) if isinstance(f.value, str) else f.value}"
                   for f in spec.filters if f.column in cols and f.op in ("=", "!=", ">", ">=", "<", "<=")]
        intent = "part_to_whole" if spec.method == "pareto" else "comparison"
        card = int(cols[dim].get("distinct") or 10) if isinstance(cols[dim].get("distinct"), int) else 10
        ctype, why = _choose(ctx, intent, cols[dim].get("semantic_type"), card, title)
        charts.append(ChartSpec(key=f"finding_{code.lower().replace('-', '_')}", title=title, chart_type=ctype, intent=intent,
                                dataset=ds.name, metric=metric, dimension=dim, filters=filters, limit=12,
                                insight_codes=[code], rationale=why, description=f"Evidence for {code}"))
    hours = next((c for c in cols if c.endswith("_hours")), None)
    if hours:
        charts.append(ChartSpec(key="dist_hours", title=f"Distribution of {cols[hours].get('label') or hours}", chart_type="histogram",
                                intent="distribution", dataset=ds.name, dimension=hours, rationale="distribution -> histogram"))
    dow = next((c for c in cols if c.endswith("_dow")), None)
    hour = next((c for c in cols if c.endswith("_hour")), None)
    if dow and hour:
        charts.append(ChartSpec(key="heat_dow_hour", title="Volume by weekday and hour", chart_type="heatmap", intent="correlation",
                                dataset=ds.name, metric="record_count", dimension=dow, series=hour, rationale="two time dimensions -> heatmap"))
    group_dim = next((c for c in cols if c.endswith("_name") and cols[c].get("semantic_type") == "categorical"), None) or \
        next((c for c in cols if cols[c].get("semantic_type") == "categorical"), None)
    if group_dim:
        charts.append(ChartSpec(key="detail_table", title=f"Operational detail by {group_dim.replace('_', ' ')}", chart_type="table",
                                intent="detail", dataset=ds.name, dimension=group_dim, metrics=[n for n in kpi_order if n in metrics][:4],
                                limit=25, rationale="operational detail -> table"))
    run_sql = ctx.run_sql(content.get("source_id"))
    dialect = content.get("dialect", "postgres")
    for chart in charts:
        try:
            sql = sqlglot.transpile(preview_sql(chart, ds.sql, metrics), read="postgres", write=dialect)[0]
            r = ctx.tools().invoke("viz.chart", {"chart": chart.key}, lambda sql=sql, key=chart.key: run_sql(sql, purpose=f"chart.preview.{key}"))
            if chart.chart_type == "histogram":
                vals = np.array([float(v[0]) for v in r.rows if v[0] is not None])
                vals = vals[vals <= np.percentile(vals, 99)] if len(vals) else vals
                counts, edges = np.histogram(vals, bins=20) if len(vals) else ([], [])
                chart.preview = {"columns": ["bin_start", "count"], "rows": [[round(float(e), 2), int(c)] for e, c in zip(edges, counts, strict=False)],
                                 "query_id": r.query_id}
            else:
                chart.preview = {"columns": r.columns, "rows": r.rows[:200], "query_id": r.query_id}
        except (AnalystOSError, ValueError) as exc:
            chart.preview = {"error": str(exc)[:300]}
    charts = [c for c in charts if "error" not in c.preview]
    by_key = {c.key: c for c in charts}
    kpis = [c.key for c in charts if c.chart_type == "kpi"]
    findings = [c.key for c in charts if c.key.startswith("finding_")]
    trends = [c.key for c in charts if c.key.startswith("trend_")]
    exec_keys = kpis + trends[:1] + findings[:3]
    ops_keys = trends[1:] + findings + [k for k in ("heat_dow_hour", "dist_hours", "detail_table") if k in by_key]
    with session_scope() as s:
        verified_rows = list(s.scalars(select(Insight).where(Insight.run_id == ctx.run.id, Insight.status == "verified")
                                       .order_by(Insight.confidence.desc())))
        summary = "\n".join(f"- **{i.title}** — {i.finding} _(confidence {i.confidence:.0%}, {i.code})_" for i in verified_rows[:5])
    dashboards = []
    for audience, keys, title in (("executive", exec_keys, "Executive overview"), ("operational", ops_keys, "Operations deep-dive")):
        if audience not in ctx.run.plan.get("audience", ["executive", "operational"]) or not keys:
            continue
        selected = [by_key[k] for k in keys]
        layout = build_layout(audience, selected)
        # Markdown/filter cells are carried by summary_markdown and native_filters; the layout lists charts only.
        layout = [c for c in (c if isinstance(c, dict) else c.model_dump() for c in layout)
                  if c.get("kind", "chart") == "chart" and c.get("chart")]
        filters = choose_native_filters(selected, ds.columns) if audience == "operational" else []
        dashboards.append(DashboardSpec(key=f"{audience}", title=f"{title}: {ctx.run.objective[:60]}", audience=audience,
                                        description=f"Generated by AnalystOS run {ctx.run.id}", charts=keys, layout=layout,
                                        native_filters=list(filters), summary_markdown=summary if audience == "executive" else ""))
    with session_scope() as s:
        ds_art = content["artifact_id"]
        chart_ids = {}
        for c in charts:
            art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="chart", name=c.key, content=c.model_dump(),
                                creator_agent=ctx.agent.id)
            chart_ids[c.key] = art.id
            link(s, ctx.workspace.id, ("chart", art.id), "visualizes", ("dataset", ds_art), run_id=ctx.run.id)
            if c.metric:
                link(s, ctx.workspace.id, ("chart", art.id), "shows_metric", ("metric", c.metric), run_id=ctx.run.id)
            for code in c.insight_codes:
                link(s, ctx.workspace.id, ("insight", code), "visualized_by", ("chart", art.id), run_id=ctx.run.id)
            if c.preview.get("query_id"):
                link(s, ctx.workspace.id, ("chart", art.id), "previewed_by", ("query", c.preview["query_id"]), run_id=ctx.run.id)
            ctx.event("chart.created", {"key": c.key, "type": c.chart_type, "title": c.title})
        for d in dashboards:
            art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="dashboard", name=d.key, content=d.model_dump(),
                                creator_agent=ctx.agent.id)
            for k in d.charts:
                link(s, ctx.workspace.id, ("chart", chart_ids[k]), "included_in", ("dashboard", art.id), run_id=ctx.run.id)
            ctx.event("dashboard.created", {"key": d.key, "charts": len(d.charts), "audience": d.audience})
    ctx.say(f"Designed {len(charts)} charts ({', '.join(sorted({c.chart_type for c in charts}))}) and {len(dashboards)} dashboards "
            f"({', '.join(d.audience for d in dashboards)}).")
    return {"charts": [c.key for c in charts], "dashboards": [d.key for d in dashboards]}


def load_bundle_parts(run_id: str) -> dict[str, Any]:
    with session_scope() as s:
        arts = list(s.scalars(select(Artifact).where(Artifact.run_id == run_id, Artifact.type.in_(["metric", "chart", "dashboard"]))
                              .order_by(Artifact.created_at)))
        return {"metrics": [MetricDef.model_validate(a.content) for a in arts if a.type == "metric"],
                "charts": [ChartSpec.model_validate(a.content) for a in arts if a.type == "chart"],
                "dashboards": [DashboardSpec.model_validate(a.content) for a in arts if a.type == "dashboard"],
                "ids": {(a.type, a.name): a.id for a in arts}}


__all__ = ["Derivation", "design", "load_bundle_parts", "preview_sql"]
