"""Run an `AnalysisSpec` end to end: compile -> governed SQL via `run_sql` -> deterministic statistics.

`run_analysis` computes the primary statistic for the spec's method. `verify_analysis` re-derives
the finding with an *independent second method* (different estimator / resampling scheme on the
same governed data) and reports whether it agrees with the primary verdict:

=================== ============================== ============================================================
method              primary                        verification (second method)
=================== ============================== ============================================================
rate_by_segment     chi-square + Cramér's V        logistic regression with segment dummies, fitted as a binomial
                                                   GLM on the aggregated table (identical to row-level logistic
                                                   regression, so no row sample and no sampling error), plus a
                                                   Monte Carlo permutation test of independence with fixed
                                                   margins (exact-in-distribution, no chi-square asymptotics).
                                                   Chosen over row-level logistic regression on a sample because
                                                   it uses all rows and handles separation (0% segments) with a
                                                   Haldane correction instead of diverging.
numeric_by_segment  Mann-Whitney / Kruskal-Wallis  bootstrap 95% CI of median(top) - median(baseline)
trend               OLS slope + change points      Mann-Kendall (Kendall tau vs time) + Theil-Sen slope
pareto              concentration + GOF            multinomial bootstrap of the top segment's share
correlation         Spearman (or Pearson)          the other coefficient + bootstrap CI of the primary one
driver_model        logistic regression            random-forest grouped permutation importance
=================== ============================== ============================================================

`agrees` is True when (primary supported) == (verification supported) and, if supported, the
verification points in the same direction (same top segment / sign).
"""
from __future__ import annotations

import datetime as _dt
import math
from collections import Counter
from decimal import Decimal
from typing import Any

import numpy as np
from pydantic import BaseModel, Field
from scipy import stats as sps

from analystos.contracts.analysis import AnalysisSpec, Derivation, StatResult
from analystos.skills import stats as st
from analystos.skills.base import RunSQL
from analystos.skills.sqlbuild import BOOLEAN_DERIVATIONS, CompiledQuery, compile_spec, describe

OTHER = "(other)"
TABLE_MAX_ROWS = 2000


class AnalysisOutcome(BaseModel):
    method: str
    stat: StatResult
    query_ids: list[str] = Field(default_factory=list)
    table: dict[str, Any] = Field(default_factory=lambda: {"columns": [], "rows": []})
    sql: list[str] = Field(default_factory=list)
    agrees: bool | None = None  # set by verify_analysis


# --------------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------------
class _Runner:
    def __init__(self, run_sql: RunSQL, spec: AnalysisSpec, sample_rows: int):
        self.run_sql = run_sql
        self.spec = spec
        self.sample_rows = sample_rows
        self.dialect = getattr(run_sql, "dialect", "duckdb")
        self.query_ids: list[str] = []
        self.sql: list[str] = []

    def run(self, purpose: str = "primary") -> tuple[CompiledQuery, list[dict[str, Any]]]:
        cq = compile_spec(self.spec, self.dialect, purpose=purpose, sample_rows=self.sample_rows)
        res = self.run_sql(cq.sql, purpose=f"analysis.{self.spec.method}.{purpose}", max_rows=cq.max_rows)
        self.query_ids.append(res.query_id)
        self.sql.append(cq.sql)
        rows = [{k.lower(): _py(v) for k, v in r.items()} for r in res.records()]
        return cq, rows


def _py(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, np.generic):
        return v.item()
    return v


def _json(v: Any) -> Any:
    v = _py(v)
    if isinstance(v, _dt.datetime | _dt.date):
        return v.isoformat()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _seg_label(v: Any, d: Derivation | None) -> str:
    if v is None:
        return "(null)"
    if d is not None and d.type in BOOLEAN_DERIVATIONS:
        return "true" if float(v) == 1 else "false"
    if isinstance(v, _dt.datetime | _dt.date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _seg_sort_key(d: Derivation | None, order: Any, label: str) -> tuple:
    if order is not None:
        return (0, float(order), label)
    if d is not None and d.type in ("hour_of_day", "day_of_week"):
        try:
            return (0, float(label), label)
        except ValueError:
            pass
    return (1, 0.0, label)


def _first(rows: list[dict[str, Any]], key: str, default: Any = 0) -> Any:
    return rows[0].get(key, default) if rows else default


def _no_data(spec: AnalysisSpec, why: str, runner: _Runner, test: str = "none") -> AnalysisOutcome:
    stat = StatResult(method=spec.method, test=test, n=0, supported=False, warnings=[why])
    return AnalysisOutcome(method=spec.method, stat=stat, query_ids=runner.query_ids, sql=runner.sql)


def _select_segments(groups: list[dict[str, Any]], spec: AnalysisSpec) -> tuple[list[dict[str, Any]], list[str], dict]:
    """Apply min_group_size and top_k (largest n first). Returns (kept, warnings, exclusion counts)."""
    warns: list[str] = []
    small = [g for g in groups if g["n"] < spec.min_group_size]
    big = sorted([g for g in groups if g["n"] >= spec.min_group_size], key=lambda g: (-g["n"], g["segment"]))
    kept, rest = big[: spec.top_k], big[spec.top_k:]
    info = {"small_segments": len(small), "small_segment_rows": sum(g["n"] for g in small),
            "beyond_top_k_segments": len(rest), "beyond_top_k_rows": sum(g["n"] for g in rest)}
    if small:
        warns.append(f"{len(small)} segment(s) with n < {spec.min_group_size} excluded from the test "
                     f"({info['small_segment_rows']} rows)")
    if rest:
        warns.append(f"only the {spec.top_k} largest segments tested; {len(rest)} more segment(s) "
                     f"({info['beyond_top_k_rows']} rows) excluded")
    return kept, warns, info


# --------------------------------------------------------------------------------------------
# per-method primaries
# --------------------------------------------------------------------------------------------
def _rate_groups(runner: _Runner) -> tuple[list[dict], dict]:
    spec = runner.spec
    _, rows = runner.run("primary")
    null_seg = sum(int(r["n_rows"] or 0) for r in rows if r["segment"] is None)
    null_out = sum(int((r["n_rows"] or 0) - (r["n"] or 0)) for r in rows if r["segment"] is not None)
    groups = [{"segment": _seg_label(r["segment"], spec.segment), "n": int(r["n"] or 0),
               "positives": int(r["positives"] or 0), "order": r.get("segment_order")}
              for r in rows if r["segment"] is not None and (r["n"] or 0) > 0]
    return groups, {"excluded_null_segment_rows": null_seg, "excluded_null_outcome_rows": null_out,
                    "rows_scanned": sum(int(r["n_rows"] or 0) for r in rows)}


def _run_rate(runner: _Runner, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    groups, excl = _rate_groups(runner)
    kept, warns, info = _select_segments(groups, spec)
    stat = st.chi_square_rates(kept, alpha=alpha)
    stat.warnings = warns + stat.warnings
    stat.details.update(excl | info | {"outcome": describe(spec.outcome), "segment": describe(spec.segment)})
    stat.highlights.update({"outcome": describe(spec.outcome), "segment_by": describe(spec.segment)})
    order = {g["segment"]: g.get("order") for g in groups}
    stat.groups.sort(key=lambda g: _seg_sort_key(spec.segment, order.get(g["segment"]), g["segment"]))
    if stat.groups and spec.segment is not None and (spec.segment.type in ("bucket", "hour_of_day", "day_of_week")):
        # ordered segments: also quote the first bucket as the natural reference level
        ref = stat.groups[0]
        top_rate = stat.highlights.get("top_rate")
        stat.highlights.update({"reference_segment": ref["segment"], "reference_rate": st._q(ref["rate"]),
                                "rate_ratio_vs_reference": st._f(top_rate / ref["rate"], 3)
                                if ref["rate"] and top_rate is not None else None})
    cols = ["segment", "n", "positives", "rate", "ci_low", "ci_high"]
    table = {"columns": cols, "rows": [[g.get(c) for c in cols] for g in stat.groups]}
    return AnalysisOutcome(method=spec.method, stat=stat, query_ids=runner.query_ids, table=table, sql=runner.sql)


def _numeric_data(runner: _Runner) -> tuple[dict[str, list[float]], list[dict], dict, list[str], dict]:
    spec = runner.spec
    _, summary = runner.run("summary")
    _, sample = runner.run("primary")
    full = {}
    null_seg = 0
    null_out = 0
    for r in summary:
        if r["segment"] is None:
            null_seg += int(r["n_rows"] or 0)
            continue
        null_out += int((r["n_rows"] or 0) - (r["n"] or 0))
        lab = _seg_label(r["segment"], spec.segment)
        full[lab] = {"segment": lab, "n": int(r["n"] or 0), "mean": r.get("mean"), "median": r.get("median"),
                     "min": r.get("min"), "max": r.get("max"), "order": r.get("segment_order")}
    kept, warns, info = _select_segments([g for g in full.values() if g["n"] > 0], spec)
    keep = {g["segment"] for g in kept}
    values: dict[str, list[float]] = {}
    for r in sample:
        lab = _seg_label(r["segment"], spec.segment)
        if lab in keep:
            values.setdefault(lab, []).append(float(r["outcome"]))
    rows_nonnull = int(_first(sample, "_rows_total", 0)) - int(_first(sample, "_rows_excluded", 0))
    excl = {"excluded_null_segment_rows": null_seg, "excluded_null_outcome_rows": null_out,
            "rows_complete": rows_nonnull, "sample_size": len(sample),
            "sampled": rows_nonnull > len(sample)} | info
    ordered = sorted(values, key=lambda s: _seg_sort_key(spec.segment, full[s].get("order"), s))
    return {s: values[s] for s in ordered}, sample, full, warns, excl


def _run_numeric(runner: _Runner, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    values, _, full, warns, excl = _numeric_data(runner)
    values = {k: v for k, v in values.items() if len(v) >= 2}
    if len(values) < 2:
        out = _no_data(spec, "need at least two segments with data", runner, "mann_whitney_u")
        out.stat.warnings = warns + out.stat.warnings
        out.stat.details.update(excl)
        return out
    stat = st.compare_groups(values, alpha=alpha)
    stat.warnings = warns + stat.warnings
    for g in stat.groups:
        f = full.get(g["segment"], {})
        g.update({"full_n": f.get("n"), "full_mean": st._f(f.get("mean")), "full_median": st._f(f.get("median"))})
    stat.details.update(excl | {"outcome": describe(spec.outcome), "segment": describe(spec.segment)})
    stat.highlights.update({"outcome": describe(spec.outcome), "segment_by": describe(spec.segment),
                            "sampled": excl["sampled"]})
    if excl["sampled"]:
        stat.warnings.append(f"test computed on a deterministic sample of {excl['sample_size']} of "
                             f"{excl['rows_complete']} complete rows; full_* columns are exact")
    cols = ["segment", "n", "median", "mean", "p25", "p75", "full_n", "full_mean", "full_median"]
    table = {"columns": cols, "rows": [[g.get(c) for c in cols] for g in stat.groups]}
    return AnalysisOutcome(method=spec.method, stat=stat, query_ids=runner.query_ids, table=table, sql=runner.sql)


def _trend_series(runner: _Runner) -> tuple[list[Any], list[float], list[dict], dict]:
    spec = runner.spec
    _, rows = runner.run("primary")
    null_period = sum(int(r["n_rows"] or 0) for r in rows if r["period"] is None)
    rows = [r for r in rows if r["period"] is not None]
    rows.sort(key=lambda r: r["period"])
    if spec.outcome is None:
        series = [(r["period"], float(r["n_rows"])) for r in rows]
    else:
        series = [(r["period"], float(r["value"])) for r in rows if (r.get("n") or 0) > 0 and r.get("value") is not None]
    return [p for p, _ in series], [v for _, v in series], rows, {"excluded_null_period_rows": null_period}


def _run_trend(runner: _Runner, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    periods, y, rows, excl = _trend_series(runner)
    stat = st.linear_trend(y, periods, alpha=alpha)
    cp = st.change_point(y, periods, alpha=alpha) if len(y) >= 6 else None
    stat.details.update(excl | {"metric": "count" if spec.outcome is None else f"mean({describe(spec.outcome)})"})
    if cp is not None:
        stat.details["change_point"] = cp.model_dump()
        stat.highlights.update({"change_points": cp.highlights.get("n_change_points", 0),
                                "change_period": cp.highlights.get("change_period"),
                                "change_before_mean": cp.highlights.get("before_mean"),
                                "change_after_mean": cp.highlights.get("after_mean")})
    counts = [float(r["n_rows"]) for r in rows]
    if len(counts) >= 3 and counts[-1] < 0.5 * float(np.median(counts)):
        stat.warnings.append("last period has < 50% of the median period volume; it may be incomplete")
    stat.highlights["metric"] = stat.details["metric"]
    cols = ["period", "n_rows", "value"]
    table = {"columns": cols, "rows": [[_json(r["period"]), r["n_rows"],
                                        _json(r.get("value")) if spec.outcome is not None else r["n_rows"]] for r in rows]}
    return AnalysisOutcome(method=spec.method, stat=stat, query_ids=runner.query_ids, table=table, sql=runner.sql)


def _pareto_items(runner: _Runner) -> tuple[list[dict], dict]:
    spec = runner.spec
    _, rows = runner.run("primary")
    null_seg = sum(int(r["n_rows"] or 0) for r in rows if r["segment"] is None)
    items = [{"segment": _seg_label(r["segment"], spec.segment), "volume": float(r["volume"] or 0)}
             for r in rows if r["segment"] is not None]
    return items, {"excluded_null_segment_rows": null_seg, "truncated_segments": len(rows) >= 5000}


def _run_pareto(runner: _Runner, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    items, excl = _pareto_items(runner)
    stat = st.pareto_concentration(items, alpha=alpha)
    stat.details.update(excl | {"segment": describe(spec.segment)})
    stat.highlights["segment_by"] = describe(spec.segment)
    top = stat.groups[: spec.top_k]
    rows = [[g["segment"], g["volume"], g["share"], g["cumulative_share"]] for g in top]
    rest = stat.groups[spec.top_k:]
    if rest:
        rows.append([OTHER, st._f(sum(g["volume"] for g in rest)), st._f(sum(g["share"] for g in rest)), 1.0])
    table = {"columns": ["segment", "volume", "share", "cumulative_share"], "rows": rows}
    return AnalysisOutcome(method=spec.method, stat=stat, query_ids=runner.query_ids, table=table, sql=runner.sql)


def _corr_pairs(runner: _Runner) -> tuple[np.ndarray, np.ndarray, dict]:
    _, rows = runner.run("primary")
    x = np.array([float(r["x"]) for r in rows])
    y = np.array([float(r["y"]) for r in rows])
    total = int(_first(rows, "_rows_total", 0))
    excl = int(_first(rows, "_rows_excluded", 0))
    return x, y, {"rows_total": total, "excluded_null_rows": excl, "sample_size": len(rows),
                  "sampled": total - excl > len(rows)}


def _corr_labels(spec: AnalysisSpec) -> tuple[str, str]:
    xd = spec.drivers[0] if spec.drivers else spec.segment
    return describe(xd), describe(spec.outcome)


def _run_corr(runner: _Runner, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    x, y, info = _corr_pairs(runner)
    xl, yl = _corr_labels(spec)
    stat = st.correlation(x, y, alpha=alpha, x_label=xl, y_label=yl)
    stat.details.update(info)
    rows = [[float(a), float(b)] for a, b in zip(x[:TABLE_MAX_ROWS], y[:TABLE_MAX_ROWS], strict=False)]
    return AnalysisOutcome(method=spec.method, stat=stat, query_ids=runner.query_ids,
                           table={"columns": [xl, yl], "rows": rows}, sql=runner.sql)


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float | Decimal) and not isinstance(v, bool)


def build_design(rows: list[dict[str, Any]], drivers: list[Derivation], *, max_levels: int = 10
                 ) -> tuple[np.ndarray, list[str], dict[str, list[int]]]:
    """Driver columns d_0..d_k -> design matrix. Numeric drivers enter as-is; categorical drivers
    are one-hot encoded (up to `max_levels` most frequent levels, the rest pooled as "(other)"),
    dropping the most frequent level as the reference."""
    cols: list[np.ndarray] = []
    names: list[str] = []
    groups: dict[str, list[int]] = {}
    used: set[str] = set()
    for i, d in enumerate(drivers):
        vals = [r[f"d_{i}"] for r in rows]
        name = describe(d)
        while name in used:
            name += "_"
        used.add(name)
        if d.type in BOOLEAN_DERIVATIONS or all(_is_number(v) for v in vals):
            groups[name] = [len(names)]
            names.append(name)
            cols.append(np.array([float(v) for v in vals]))
            continue
        labels = [_seg_label(v, d) for v in vals]
        freq = Counter(labels).most_common()
        top = [lv for lv, _ in freq[:max_levels]]
        labels = [lv if lv in top else OTHER for lv in labels]
        levels = [lv for lv, _ in Counter(labels).most_common()]
        idx = []
        for lv in levels[1:]:
            idx.append(len(names))
            names.append(f"{name}={lv}")
            cols.append(np.array([1.0 if x == lv else 0.0 for x in labels]))
        groups[name] = idx
    X = np.column_stack(cols) if cols else np.zeros((len(rows), 0))
    return X, names, groups


def _driver_data(runner: _Runner) -> tuple[np.ndarray, np.ndarray, list[str], dict[str, list[int]], dict]:
    spec = runner.spec
    _, rows = runner.run("primary")
    X, names, groups = build_design(rows, spec.drivers)
    y = np.array([float(r["outcome"]) for r in rows])
    total = int(_first(rows, "_rows_total", 0))
    excl = int(_first(rows, "_rows_excluded", 0))
    return X, y, names, groups, {"rows_total": total, "excluded_null_rows": excl, "sample_size": len(rows),
                                 "sampled": total - excl > len(rows)}


def _run_driver(runner: _Runner, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    X, y, names, groups, info = _driver_data(runner)
    if X.shape[0] == 0:
        return _no_data(spec, "no complete rows", runner, "logistic_regression")
    stat = st.logistic_regression(X, y, names, feature_groups=groups, alpha=alpha)
    stat.details.update(info | {"feature_groups": groups, "outcome": describe(spec.outcome)})
    stat.highlights["outcome"] = describe(spec.outcome)
    cols = ["feature", "odds_ratio", "ci_low", "ci_high", "p_value"]
    table = {"columns": cols, "rows": [[g.get(c) for c in cols] for g in stat.groups]}
    return AnalysisOutcome(method=spec.method, stat=stat, query_ids=runner.query_ids, table=table, sql=runner.sql)


_PRIMARY = {"rate_by_segment": _run_rate, "numeric_by_segment": _run_numeric, "trend": _run_trend,
            "pareto": _run_pareto, "correlation": _run_corr, "driver_model": _run_driver}


def run_analysis(spec: AnalysisSpec, run_sql: RunSQL, *, alpha: float = 0.05, sample_rows: int = 50000) -> AnalysisOutcome:
    """Compile `spec`, fetch aggregates/samples through `run_sql`, and compute the primary statistic."""
    runner = _Runner(run_sql, spec, sample_rows)
    return _PRIMARY[spec.method](runner, alpha)


# --------------------------------------------------------------------------------------------
# verification (independent second method)
# --------------------------------------------------------------------------------------------
def _verdict(primary: StatResult, supported: bool, direction_agrees: bool | None) -> bool:
    if primary.supported:
        return bool(supported and direction_agrees)
    return not supported


def _finish(spec: AnalysisSpec, runner: _Runner, primary: StatResult, v: StatResult, direction: bool | None,
            table: dict[str, Any]) -> AnalysisOutcome:
    agrees = _verdict(primary, bool(v.supported), direction)
    v.details.update({"agrees": agrees, "direction_agrees": direction, "primary_supported": primary.supported})
    return AnalysisOutcome(method=spec.method, stat=v, query_ids=runner.query_ids, table=table, sql=runner.sql, agrees=agrees)


def _verify_rate(runner: _Runner, primary: StatResult, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    groups, _ = _rate_groups(runner)
    kept, _, _ = _select_segments(groups, spec)
    top, base = primary.highlights.get("top_segment"), primary.highlights.get("baseline_segment")
    segs = {g["segment"] for g in kept}
    if len(kept) < 2 or top not in segs or base not in segs or top == base:
        v = StatResult(method=spec.method, test="grouped_logistic_regression", n=sum(g["n"] for g in kept), supported=False,
                       warnings=["primary top/baseline segments unavailable; nothing to verify"])
        return _finish(spec, runner, primary, v, None, {"columns": [], "rows": []})
    gl = st.grouped_logistic(kept, baseline=base, alpha=alpha)
    perm = st.permutation_chi_square(kept, n_perm=2000, seed=0)
    o = gl["odds_ratios"][top]
    lr_p, perm_p = gl["lr_p_value"], perm["p_value"]
    direction = o["odds_ratio"] is not None and o["odds_ratio"] > 1
    ci_excl = o["ci_low"] is not None and o["ci_low"] > 1
    supported = st._sig(lr_p, alpha) and st._sig(perm_p, alpha) and ci_excl and o["odds_ratio"] >= st.MIN_ODDS_RATIO
    v = StatResult(method=spec.method, test="grouped_logistic_regression+permutation_chi_square",
                   n=sum(g["n"] for g in kept), statistic=gl["lr_statistic"], p_value=lr_p, effect_size=o["odds_ratio"],
                   effect_label="odds_ratio_top_vs_baseline", ci_low=o["ci_low"], ci_high=o["ci_high"],
                   groups=[{"segment": s, **d} for s, d in gl["odds_ratios"].items()],
                   highlights={"top_segment": top, "baseline_segment": base, "odds_ratio": st._q(o["odds_ratio"]),
                               "odds_ratio_ci": [st._q(o["ci_low"]), st._q(o["ci_high"])], "lr_p_value": lr_p,
                               "permutation_p_value": perm_p},
                   warnings=gl["warnings"], supported=supported,
                   details={"permutation": perm, "alpha": alpha, "threshold_odds_ratio": st.MIN_ODDS_RATIO})
    table = {"columns": ["segment", "odds_ratio", "ci_low", "ci_high", "p_value"],
             "rows": [[s, d["odds_ratio"], d["ci_low"], d["ci_high"], d["p_value"]] for s, d in gl["odds_ratios"].items()]}
    return _finish(spec, runner, primary, v, direction, table)


def _cap(values: list[float], k: int, seed: int = 0) -> np.ndarray:
    a = np.asarray(values, dtype=float)
    if a.size <= k:
        return a
    return np.random.default_rng(seed).choice(a, size=k, replace=False)


def _verify_numeric(runner: _Runner, primary: StatResult, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    values, _, _, _, _ = _numeric_data(runner)
    top, base = primary.highlights.get("top_segment"), primary.highlights.get("baseline_segment")
    if top not in values or base not in values or top == base:
        v = StatResult(method=spec.method, test="bootstrap_median_difference", n=0, supported=False,
                       warnings=["primary top/baseline segments unavailable; nothing to verify"])
        return _finish(spec, runner, primary, v, None, {"columns": [], "rows": []})
    a, b = _cap(values[top], 20000, 1), _cap(values[base], 20000, 2)
    est, lo, hi = st.bootstrap_diff_ci(a, b, np.median, n=2000, seed=0, alpha=alpha)
    base_med = float(np.median(b))
    rel = est / abs(base_med) if base_med != 0 else None
    supported = lo > 0 or hi < 0
    v = StatResult(method=spec.method, test="bootstrap_median_difference", n=int(a.size + b.size), statistic=st._f(est),
                   effect_size=st._f(rel), effect_label="relative_median_difference", ci_low=st._f(lo), ci_high=st._f(hi),
                   highlights={"top_segment": top, "baseline_segment": base, "median_difference": st._q(est),
                               "median_difference_ci": [st._q(lo), st._q(hi)], "relative_difference": st._q(rel)},
                   assumptions=["percentile bootstrap, 2000 resamples, seed 0"], supported=supported,
                   details={"alpha": alpha})
    table = {"columns": ["top_segment", "baseline_segment", "median_difference", "ci_low", "ci_high"],
             "rows": [[top, base, st._f(est), st._f(lo), st._f(hi)]]}
    return _finish(spec, runner, primary, v, est > 0, table)


def _verify_trend(runner: _Runner, primary: StatResult, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    periods, y, _, _ = _trend_series(runner)
    n = len(y)
    if n < 4:
        v = StatResult(method=spec.method, test="mann_kendall", n=n, supported=False, warnings=["need at least 4 periods"])
        return _finish(spec, runner, primary, v, None, {"columns": [], "rows": []})
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
    return _finish(spec, runner, primary, v, direction, table)


def _verify_pareto(runner: _Runner, primary: StatResult, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    items, _ = _pareto_items(runner)
    items = [i for i in items if i["volume"] > 0]
    top = primary.highlights.get("top_segment")
    segs = [i["segment"] for i in items]
    if len(items) < 2 or top not in segs:
        v = StatResult(method=spec.method, test="multinomial_bootstrap_top_share", n=0, supported=False,
                       warnings=["primary top segment unavailable; nothing to verify"])
        return _finish(spec, runner, primary, v, None, {"columns": [], "rows": []})
    vols = np.array([round(i["volume"]) for i in items], dtype=np.int64)
    total = int(vols.sum())
    p = vols / total
    rng = np.random.default_rng(0)
    sims = rng.multinomial(total, p, size=2000)
    ti = segs.index(top)
    shares = sims[:, ti] / total
    stability = float((sims.argmax(axis=1) == ti).mean())
    k = len(items)
    n20 = max(1, math.ceil(0.2 * k))
    top20 = np.sort(sims, axis=1)[:, ::-1][:, :n20].sum(axis=1) / total
    lo, hi = np.quantile(shares, [alpha / 2, 1 - alpha / 2])
    lo20 = float(np.quantile(top20, alpha / 2))
    fair = 1 / k
    supported = lo >= st.MIN_TOP_SHARE_RATIO * fair or lo20 >= st.MIN_TOP20_SHARE
    v = StatResult(method=spec.method, test="multinomial_bootstrap_top_share", n=total, statistic=st._f(p[ti]),
                   effect_size=st._f(p[ti] / fair), effect_label="top_share_vs_fair_share", ci_low=st._f(lo), ci_high=st._f(hi),
                   highlights={"top_segment": top, "top_share": st._q(p[ti]), "top_share_ci": [st._q(lo), st._q(hi)],
                               "top_segment_stability": st._q(stability), "top_20pct_share_ci_low": st._q(lo20)},
                   assumptions=["records resampled with replacement (multinomial), 2000 resamples, seed 0"],
                   supported=supported, details={"alpha": alpha, "fair_share": st._f(fair)})
    table = {"columns": ["top_segment", "share", "ci_low", "ci_high", "stability"],
             "rows": [[top, st._f(p[ti]), st._f(lo), st._f(hi), st._f(stability)]]}
    return _finish(spec, runner, primary, v, stability >= 0.5, table)


def _verify_corr(runner: _Runner, primary: StatResult, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    x, y, _ = _corr_pairs(runner)
    prim_method = primary.test if primary.test in ("spearman", "pearson") else "spearman"
    other = "pearson" if prim_method == "spearman" else "spearman"
    xl, yl = _corr_labels(spec)
    o = st.correlation(x, y, method=other, alpha=alpha, x_label=xl, y_label=yl)
    if o.statistic is None:
        return _finish(spec, runner, primary, o, None, {"columns": [], "rows": []})
    ok = np.isfinite(x) & np.isfinite(y)
    xs, ys = x[ok], y[ok]
    if xs.size > 5000:
        idx = np.random.default_rng(0).choice(xs.size, 5000, replace=False)
        xs, ys = xs[idx], ys[idx]
    if prim_method == "spearman":
        xs, ys = sps.rankdata(xs), sps.rankdata(ys)
    rng = np.random.default_rng(0)
    boots = []
    for _ in range(1000):
        i = rng.integers(0, xs.size, xs.size)
        a, b = xs[i], ys[i]
        if np.ptp(a) == 0 or np.ptp(b) == 0:
            continue
        boots.append(np.corrcoef(a, b)[0, 1])
    lo, hi = (np.quantile(boots, [alpha / 2, 1 - alpha / 2]) if boots else (np.nan, np.nan))
    excl0 = bool(lo > 0 or hi < 0)
    supported = bool(o.supported) and excl0
    direction = (o.statistic > 0) == ((primary.statistic or 0) > 0)
    o.test = f"{other}+bootstrap_{prim_method}"
    o.supported = supported
    o.details["bootstrap_primary_ci"] = [st._f(lo), st._f(hi)]
    o.highlights["bootstrap_primary_ci"] = [st._q(lo), st._q(hi)]
    table = {"columns": ["coefficient", "value", "ci_low", "ci_high"],
             "rows": [[other, o.statistic, o.ci_low, o.ci_high], [f"bootstrap_{prim_method}", primary.statistic,
                                                                   st._f(lo), st._f(hi)]]}
    return _finish(spec, runner, primary, o, direction, table)


def _verify_driver(runner: _Runner, primary: StatResult, alpha: float) -> AnalysisOutcome:
    spec = runner.spec
    X, y, names, groups, _ = _driver_data(runner)
    fi = st.feature_importance(X, y, names, feature_groups=groups, seed=0)
    top_lr = primary.highlights.get("top_driver")
    ranking = fi.highlights.get("ranking") or []
    direction = top_lr in ranking[:2] if top_lr else None
    fi.details["primary_top_driver"] = top_lr
    table = {"columns": ["driver", "importance_mean", "importance_std", "rank"],
             "rows": [[g["driver"], g["importance_mean"], g["importance_std"], g["rank"]] for g in fi.groups]}
    return _finish(spec, runner, primary, fi, direction, table)


_VERIFY = {"rate_by_segment": _verify_rate, "numeric_by_segment": _verify_numeric, "trend": _verify_trend,
           "pareto": _verify_pareto, "correlation": _verify_corr, "driver_model": _verify_driver}


def verify_analysis(spec: AnalysisSpec, run_sql: RunSQL, primary: StatResult, *, alpha: float = 0.05,
                    sample_rows: int = 50000) -> AnalysisOutcome:
    """Re-derive the finding with an independent second method; `outcome.agrees` is the verdict."""
    runner = _Runner(run_sql, spec, sample_rows)
    return _VERIFY[spec.method](runner, primary, alpha)
