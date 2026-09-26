"""Method-specific evaluation (P4-03; extends the P4-V01 harness): null behaviour, power near the
materiality threshold and selection effects, per analysis method, by seeded Monte Carlo over the
verdict layer the methods use (`skills.stats`). No services.

* **Null** — the share of null datasets a method calls *supported* (its verdict, after its effect-size
  gate), per method; it must not exceed the nominal α beyond Monte Carlo error.
* **Power near the threshold** — the supported rate as the true effect moves from half to three times the
  method's materiality threshold (`MIN_RATE_RATIO`, `MIN_RANK_BISERIAL`, `MIN_ABS_RHO`, `MIN_TREND_PCT`).
  An effect exactly at the threshold is found about half the time *by construction* (the point estimate
  must clear the threshold): the platform is built to report material effects, not every real one.
* **Selection** — (a) the winner's curse: the quoted top-vs-bottom rate ratio after picking the top and
  bottom groups post hoc, under the null and with one planted group; (b) testing a post-hoc selected
  group on the same data versus on an untouched half (the discovery/confirmation rule of P4-03).

Methods not simulated here (pareto, driver_model, cohort_retention, contribution_decomposition) keep
their planted/null tests in `tests/methods` and `tests/unit/test_skills_analysis.py`; the platform-level
null FDR for every method comes from the V01 component suite (`evaluation.analytical`, `by_method`).
"""
from __future__ import annotations

import math
import time
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from scipy.stats import norm

ALPHA = 0.05
MULTIPLES = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0)
# Monte Carlo tolerance for a rate of α over `reps` datasets: α + 2.5 standard errors (0.08 at 200 reps).
THRESHOLDS = {"null_supported_max": 0.08, "power_at_2x_min": 0.80, "holdout_fpr_max": 0.08, "same_data_fpr_min": 0.10}


@dataclass
class MethodResult:
    method: str
    threshold: str
    design: str
    power: dict[str, float] = field(default_factory=dict)  # "<multiple>x" -> supported rate
    null_supported: float = 0.0
    null_raw_p: float = 0.0  # share with raw p < α (test calibration before the effect gate)


def _rate(rng: np.random.Generator, k: float, reps: int, n: int) -> MethodResult:
    from analystos.skills import stats as st

    rr = 1 + (st.MIN_RATE_RATIO - 1) * k
    out = MethodResult("rate_by_segment", f"rate ratio >= {st.MIN_RATE_RATIO} (and Cramér's V >= {st.MIN_CRAMERS_V})",
                       f"4 groups x {n}, baseline rate 0.20, one group at 0.20 x (1 + {st.MIN_RATE_RATIO - 1:.2f} k)")
    sup = raw = 0
    for _ in range(reps):
        rates = [0.2, 0.2, 0.2, min(0.2 * rr, 0.99)]
        r = st.chi_square_rates([{"segment": str(i), "n": n, "positives": int(rng.binomial(n, p))} for i, p in enumerate(rates)],
                                alpha=ALPHA)
        sup += bool(r.supported)
        raw += r.p_value is not None and r.p_value < ALPHA
    out.power[f"{k:g}x"], out.null_raw_p = sup / reps, raw / reps
    return out


def _numeric(rng: np.random.Generator, k: float, reps: int, n: int) -> MethodResult:
    from analystos.skills import stats as st

    target = st.MIN_RANK_BISERIAL * k
    shift = math.sqrt(2) * norm.ppf((1 + target) / 2)  # normal shift whose rank-biserial is `target`
    out = MethodResult("numeric_by_segment", f"|rank-biserial| >= {st.MIN_RANK_BISERIAL}",
                       f"2 groups x {n}, normal(0,1) vs normal(shift,1), shift for rank-biserial {st.MIN_RANK_BISERIAL} k")
    sup = raw = 0
    for _ in range(reps):
        r = st.mann_whitney(rng.normal(shift, 1, n), rng.normal(0, 1, n), alpha=ALPHA)
        sup += bool(r.supported)
        raw += r.p_value is not None and r.p_value < ALPHA
    out.power[f"{k:g}x"], out.null_raw_p = sup / reps, raw / reps
    return out


def _correlation(rng: np.random.Generator, k: float, reps: int, n: int) -> MethodResult:
    from analystos.skills import stats as st

    rho_s = st.MIN_ABS_RHO * k
    r_p = 2 * math.sin(math.pi * rho_s / 6)  # Pearson r of a bivariate normal with Spearman rho_s
    out = MethodResult("correlation", f"|Spearman rho| >= {st.MIN_ABS_RHO}",
                       f"{n} pairs, bivariate normal with Spearman rho {st.MIN_ABS_RHO} k")
    sup = raw = 0
    for _ in range(reps):
        x = rng.normal(size=n)
        y = r_p * x + math.sqrt(max(1 - r_p ** 2, 0)) * rng.normal(size=n)
        r = st.correlation(x, y, alpha=ALPHA)
        sup += bool(r.supported)
        raw += r.p_value is not None and r.p_value < ALPHA
    out.power[f"{k:g}x"], out.null_raw_p = sup / reps, raw / reps
    return out


def _trend(rng: np.random.Generator, k: float, reps: int, n: int) -> MethodResult:
    from analystos.skills import stats as st

    pct = st.MIN_TREND_PCT * k
    out = MethodResult("trend", f"|fitted change| >= {st.MIN_TREND_PCT:.0%} first to last period",
                       f"{n} periods, level 100 + noise sd 5, fitted change {st.MIN_TREND_PCT:.0%} k")
    sup = raw = 0
    t = np.arange(n)
    for _ in range(reps):
        y = 100 * (1 + pct * t / (n - 1)) + rng.normal(0, 5, n)
        r = st.linear_trend(y, alpha=ALPHA)
        sup += bool(r.supported)
        raw += r.p_value is not None and r.p_value < ALPHA
    out.power[f"{k:g}x"], out.null_raw_p = sup / reps, raw / reps
    return out


SIMULATORS = {"rate_by_segment": (_rate, 2000), "numeric_by_segment": (_numeric, 1500), "correlation": (_correlation, 1500),
              "trend": (_trend, 26)}


def power_and_null(reps: int = 200, seed: int = 20260926) -> list[MethodResult]:
    results = []
    for name, (sim, n) in SIMULATORS.items():
        merged: MethodResult | None = None
        for i, k in enumerate(MULTIPLES):
            r = sim(np.random.default_rng(seed + 1000 * i + zlib.crc32(name.encode()) % 997), k, reps, n)
            if merged is None:
                merged = r
            merged.power.update(r.power)
            if k == 0:
                merged.null_supported, merged.null_raw_p = r.power["0x"], r.null_raw_p
        assert merged is not None
        results.append(merged)
    return results


def selection(reps: int = 400, seed: int = 7, groups: int = 8, n: int = 1000) -> dict[str, Any]:
    """Winner's curse on the quoted rate ratio, and same-data vs held-out testing of a post-hoc top group."""
    from analystos.skills import stats as st

    rng = np.random.default_rng(seed)

    def rate_groups(rates: list[float], size: int) -> list[dict[str, Any]]:
        return [{"segment": str(i), "n": size, "positives": int(rng.binomial(size, p))} for i, p in enumerate(rates)]

    null_rr, planted_rr, planted_sup = [], [], []
    for _ in range(reps):
        r0 = st.chi_square_rates(rate_groups([0.2] * groups, n), alpha=ALPHA)
        null_rr.append(r0.highlights.get("rate_ratio") or 1.0)
        r1 = st.chi_square_rates(rate_groups([0.2] * (groups - 1) + [0.3], n), alpha=ALPHA)
        if r1.supported:
            planted_sup.append(r1)
            planted_rr.append(r1.highlights.get("rate_ratio"))
    true_rr = 0.3 / 0.2

    same = held = 0
    for _ in range(reps):
        half_a = rate_groups([0.2] * groups, n // 2)
        half_b = rate_groups([0.2] * groups, n // 2)
        both = [{"segment": a["segment"], "n": a["n"] + b["n"], "positives": a["positives"] + b["positives"]}
                for a, b in zip(half_a, half_b, strict=True)]

        def top_vs_rest(sample: list[dict[str, Any]], top: str) -> float:
            t = next(g for g in sample if g["segment"] == top)
            rest = {"segment": "rest", "n": sum(g["n"] for g in sample if g is not t),
                    "positives": sum(g["positives"] for g in sample if g is not t)}
            return st.chi_square_rates([t, rest], alpha=ALPHA).p_value or 1.0

        top_all = max(both, key=lambda g: g["positives"] / g["n"])["segment"]
        top_a = max(half_a, key=lambda g: g["positives"] / g["n"])["segment"]
        same += top_vs_rest(both, top_all) < ALPHA
        held += top_vs_rest(half_b, top_a) < ALPHA
    return {"design": f"{groups} groups x {n} rows, rate 0.20 (null) / one group at 0.30 (planted); {reps} datasets",
            "null_quoted_rate_ratio_mean": round(float(np.mean(null_rr)), 3),
            "null_quoted_rate_ratio_p95": round(float(np.percentile(null_rr, 95)), 3),
            "planted_true_rate_ratio": round(true_rr, 3),
            "planted_quoted_rate_ratio_mean_when_supported": round(float(np.mean(planted_rr)), 3) if planted_rr else None,
            "planted_supported_rate": round(len(planted_sup) / reps, 3),
            "same_data_fpr": round(same / reps, 3), "held_out_fpr": round(held / reps, 3)}


@dataclass
class Report:
    reps: int
    seconds: float
    methods: list[MethodResult]
    selection: dict[str, Any]
    by_method_v01: dict[str, Any] | None = None


def run(reps: int = 200, seed: int = 20260926, *, v01_seeds: list[int] | None = None,
        v01_null_seeds: list[int] | None = None) -> Report:
    started = time.perf_counter()
    methods = power_and_null(reps, seed)
    sel = selection(reps=max(reps * 2, 200))
    by_method = None
    if v01_seeds is not None or v01_null_seeds is not None:
        from evaluation.analytical import run_component_suite

        summary, _ = run_component_suite(v01_seeds or [], null_seeds=v01_null_seeds or [])
        by_method = summary.by_method
    return Report(reps=reps, seconds=round(time.perf_counter() - started, 1), methods=methods, selection=sel,
                  by_method_v01=by_method)


def check(report: Report, thresholds: dict[str, float] = THRESHOLDS) -> list[str]:
    problems = []
    for m in report.methods:
        if m.null_supported > thresholds["null_supported_max"]:
            problems.append(f"{m.method}: supported under the null {m.null_supported} > {thresholds['null_supported_max']}")
        if m.power.get("2x", 0) < thresholds["power_at_2x_min"]:
            problems.append(f"{m.method}: power at 2x the threshold {m.power.get('2x')} < {thresholds['power_at_2x_min']}")
    sel = report.selection
    if sel["held_out_fpr"] > thresholds["holdout_fpr_max"]:
        problems.append(f"selection: held-out FPR {sel['held_out_fpr']} > {thresholds['holdout_fpr_max']}")
    if sel["same_data_fpr"] < thresholds["same_data_fpr_min"]:
        problems.append(f"selection: same-data FPR {sel['same_data_fpr']} < {thresholds['same_data_fpr_min']} "
                        "(the harness no longer detects the selection effect)")
    for name, row in (report.by_method_v01 or {}).items():
        if row.get("null_fdr", 0) > ALPHA:
            problems.append(f"V01 {name}: FDR under the global null {row['null_fdr']} > {ALPHA}")
    return problems


def as_dict(report: Report) -> dict[str, Any]:
    return asdict(report)
