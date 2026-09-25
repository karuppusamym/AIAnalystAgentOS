"""trend: volume (no outcome) or a numeric outcome's mean over `time` periods.

primary       OLS slope over the period index + binary-segmentation change points
verification  Mann-Kendall (Kendall tau vs time) + Theil-Sen slope
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy import stats as sps
from sqlglot import exp

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.methods.base import (
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    ColumnType,
    Rows,
    Semantic,
    cap,
    finish,
    json_value,
    nothing_to_verify,
    text_parts,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st


def series(rows: list[dict[str, Any]], spec: AnalysisSpec) -> tuple[list[Any], list[float], list[dict], dict]:
    null_period = sum(int(r["n_rows"] or 0) for r in rows if r["period"] is None)
    rows = sorted((r for r in rows if r["period"] is not None), key=lambda r: r["period"])
    if spec.outcome is None:
        pts = [(r["period"], float(r["n_rows"])) for r in rows]
    else:
        pts = [(r["period"], float(r["value"])) for r in rows if (r.get("n") or 0) > 0 and r.get("value") is not None]
    return [p for p, _ in pts], [v for _, v in pts], rows, {"excluded_null_period_rows": null_period}


class Trend(AnalysisMethod):
    name = "trend"
    vocabulary = "volume (outcome null) or numeric outcome over `time` (time.type = date_trunc, grain week|month)."
    playbook = "volume_trend"
    outcome_types = frozenset({"numeric"})

    def applicable(self, outcome: ColumnType | None, segment: ColumnType | None) -> bool:
        return segment is None and outcome in (None, "numeric")

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        if spec.time is None or spec.time.type != "date_trunc" or not spec.time.grain:
            return [f"{self.name} needs time = date_trunc with a grain"]
        return []

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        sb.require(spec, "time")
        assert spec.time is not None
        notes: list[str] = []
        period = sb.time_period_expr(spec.time, dialect)
        cols: list[tuple[str, exp.Expression]] = [("period", period)]
        if spec.outcome is not None:
            cols.append(("outcome", sb.as_double(sb.derive(spec.outcome, dialect), dialect)))
        inner = sb.base_select(spec, dialect, cols)
        aggs = [sb.count_star().as_(sb.ident("n_rows"))]
        columns = {"period": "period", "n_rows": "n_rows"}
        if spec.outcome is not None:
            aggs += [exp.Count(this=sb.ref("outcome")).as_(sb.ident("n")), exp.Avg(this=sb.ref("outcome")).as_(sb.ident("value"))]
            columns.update({"n": "n", "value": "value"})
        if spec.segment is not None:
            notes.append(f"{self.name} ignores `segment`; use one spec per segment value (filters) to split series")
        q = (exp.select(sb.ref("period"), *aggs).from_(sb.subquery(inner, "d")).group_by(sb.ref("period"))
             .order_by(sb.ref("period")).limit(sb.MAX_PERIODS))
        notes.append("rows with NULL period form a NULL group and are excluded from the series")
        return sb.CompiledQuery(sb.to_sql(q, dialect), columns, notes, purpose, dialect, sb.MAX_PERIODS, "aggregate")

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        periods, y, raw, excl = series(rows["primary"], spec)
        stat = st.linear_trend(y, periods, alpha=alpha)
        cp = st.change_point(y, periods, alpha=alpha) if len(y) >= 6 else None
        stat.details.update(excl | {"metric": "count" if spec.outcome is None else f"mean({sb.describe(spec.outcome)})"})
        if cp is not None:
            stat.details["change_point"] = cp.model_dump()
            stat.highlights.update({"change_points": cp.highlights.get("n_change_points", 0),
                                    "change_period": cp.highlights.get("change_period"),
                                    "change_before_mean": cp.highlights.get("before_mean"),
                                    "change_after_mean": cp.highlights.get("after_mean")})
        counts = [float(r["n_rows"]) for r in raw]
        if len(counts) >= 3 and counts[-1] < 0.5 * float(np.median(counts)):
            stat.warnings.append("last period has < 50% of the median period volume; it may be incomplete")
        stat.highlights["metric"] = stat.details["metric"]
        cols = ["period", "n_rows", "value"]
        table = {"columns": cols, "rows": [[json_value(r["period"]), r["n_rows"],
                                            json_value(r.get("value")) if spec.outcome is not None else r["n_rows"]] for r in raw]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        _, y, _, _ = series(rows["primary"], spec)
        n = len(y)
        if n < 4:
            return nothing_to_verify(spec, primary, "mann_kendall", "need at least 4 periods", n)
        t = np.arange(n, dtype=float)
        ya = np.asarray(y)
        tau, p = sps.kendalltau(t, ya)
        ts = sps.theilslopes(ya, t)
        f0 = ts.intercept
        f1 = ts.intercept + ts.slope * (n - 1)
        pct = (f1 - f0) / abs(f0) if f0 != 0 else None
        supported = st._sig(p, alpha) and pct is not None and abs(pct) >= st.MIN_TREND_PCT
        slope = primary.statistic or 0.0
        direction = (tau > 0) == (slope > 0) if tau is not None and math.isfinite(tau) else None
        v = StatResult(method=spec.method, test="mann_kendall+theil_sen", n=n, statistic=st._f(tau), p_value=st._f(p, 12),
                       effect_size=st._f(pct), effect_label="pct_change_theil_sen", ci_low=st._f(ts.low_slope),
                       ci_high=st._f(ts.high_slope),
                       highlights={"kendall_tau": st._q(tau), "theil_sen_slope": st._q(ts.slope), "pct_change": st._q(pct),
                                   "p_value": st._f(p, 8)},
                       assumptions=["monotonic trend", "ci is the Theil-Sen slope CI"], supported=supported,
                       details={"alpha": alpha})
        table = {"columns": ["kendall_tau", "p_value", "theil_sen_slope", "pct_change"],
                 "rows": [[st._f(tau), st._f(p, 12), st._f(ts.slope), st._f(pct)]]}
        return finish(spec, primary, v, direction, table)

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        _, out, scope = text_parts(spec)
        title = f"{cap(out) if out != 'volume' else 'Volume'} shows a significant trend"
        text = (f"Weekly {out} changed by {hl.get('pct_change', 0):.1f}% from first to last period"
                if isinstance(hl.get("pct_change"), (int, float)) else f"A significant trend was detected in {out}") + f"{scope}."
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="trend", dimension="time", measure="volume" if spec.outcome is None else "outcome")
