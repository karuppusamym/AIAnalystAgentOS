"""Deterministic statistical skills. Pure functions: numbers in, `StatResult` out.

Every number an insight is allowed to quote comes from one of these functions. None of them
touch a database or a model; randomness (bootstrap, permutation, random forest) always uses a
fixed seed so the same input gives the same output.

Verdict thresholds (``supported``)
----------------------------------
A result is ``supported`` only when it is statistically significant (p < ``alpha``, default 0.05)
AND the effect is large enough to matter. Significance alone is not enough: on the row counts
typical of operational data (10^4-10^6) trivially small differences are "significant".

==========================  ==========================================================================
function                    minimum effect (defaults; all overridable by keyword)
==========================  ==========================================================================
chi_square_rates            Cramér's V >= 0.05 AND worst/best rate ratio >= 1.2
mann_whitney                |rank-biserial r| >= 0.10
kruskal_wallis              epsilon² >= 0.01
welch_t_test                |Hedges g| >= 0.20
one_way_anova               eta² >= 0.01
correlation                 |rho| (or |r|) >= 0.10
logistic_regression         model LR-test p < alpha AND one feature with p < alpha and OR >= 1.2 (or <= 1/1.2)
feature_importance          held-out AUC >= 0.55 (R² >= 0.02 for regression) AND top permutation
                            importance lower bound (mean - 2 sd) > 0.005
linear_trend                |fitted % change over the window| >= 10%
change_point                shift p < alpha / (#candidate splits) AND |relative shift| >= 10%
pareto_concentration        goodness-of-fit vs uniform p < alpha AND (top segment >= 2x its fair share
                            OR the top 20% of segments hold >= 50% of volume)
robust_anomalies            any |modified z| >= 3.5 (Iglewicz & Hoaglin)
==========================  ==========================================================================
"""
from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
from scipy import stats as sps

from analystos.contracts.analysis import StatResult

Z95 = 1.959963984540054

# Documented default thresholds (see module docstring).
MIN_CRAMERS_V = 0.05
MIN_RATE_RATIO = 1.2
MIN_RANK_BISERIAL = 0.10
MIN_EPSILON_SQ = 0.01
MIN_HEDGES_G = 0.20
MIN_ETA_SQ = 0.01
MIN_ABS_RHO = 0.10
MIN_ODDS_RATIO = 1.2
MIN_AUC = 0.55
MIN_R2 = 0.02
MIN_PERM_IMPORTANCE = 0.005
MIN_TREND_PCT = 0.10
MIN_SHIFT_PCT = 0.10
MIN_TOP_SHARE_RATIO = 2.0
MIN_TOP20_SHARE = 0.5
ROBUST_Z = 3.5


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def _f(x: Any, nd: int = 10) -> float | None:
    """Round to a JSON-safe float (None for NaN/inf/None)."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, nd)


def _q(x: Any) -> float | None:
    """Human-quotable rounding for highlights (4 decimals)."""
    return _f(x, 4)


def _arr(values: Sequence[float] | np.ndarray) -> np.ndarray:
    a = np.asarray(list(values) if not isinstance(values, np.ndarray) else values, dtype=float)
    return a[np.isfinite(a)]


def _sig(p: float | None, alpha: float) -> bool:
    return p is not None and math.isfinite(p) and p < alpha


def wilson_ci(k: float, n: float, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z / denom * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def fisher_z_ci(r: float, n: int, *, spearman: bool = False, z: float = Z95) -> tuple[float | None, float | None]:
    """Fisher-z CI for a correlation. For Spearman uses the Fieller et al. variance 1.06/(n-3)."""
    if n <= 3 or r is None or not math.isfinite(r):
        return (None, None)
    rr = max(min(r, 0.999999), -0.999999)
    se = math.sqrt((1.06 if spearman else 1.0) / (n - 3))
    zr = math.atanh(rr)
    return (math.tanh(zr - z * se), math.tanh(zr + z * se))


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg FDR-adjusted p-values (same order as input, monotone, capped at 1)."""
    p = np.asarray(p_values, dtype=float)
    m = p.size
    if m == 0:
        return []
    order = np.argsort(p, kind="mergesort")
    ranked = p[order] * m / np.arange(1, m + 1)
    adj = np.minimum.accumulate(ranked[::-1])[::-1]
    adj = np.minimum(adj, 1.0)
    out = np.empty(m)
    out[order] = adj
    return [float(v) for v in out]


def _apply_stat(stat: Callable, samples: np.ndarray) -> np.ndarray:
    try:
        r = np.asarray(stat(samples, axis=1), dtype=float)
        if r.shape == (samples.shape[0],):
            return r
    except TypeError:
        pass
    return np.array([float(stat(row)) for row in samples])


def _bootstrap_dist(values: np.ndarray, stat: Callable, n: int, rng: np.random.Generator) -> np.ndarray:
    size = values.size
    chunk = max(1, min(n, 2_000_000 // max(size, 1)))
    out = []
    done = 0
    while done < n:
        c = min(chunk, n - done)
        idx = rng.integers(0, size, size=(c, size))
        out.append(_apply_stat(stat, values[idx]))
        done += c
    return np.concatenate(out)


def bootstrap_ci(values: Sequence[float], stat: Callable = np.median, n: int = 2000, seed: int = 0,
                 alpha: float = 0.05) -> tuple[float, float, float]:
    """Percentile bootstrap: (estimate, ci_low, ci_high). `stat` may accept `axis=` for speed."""
    a = _arr(values)
    if a.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    dist = _bootstrap_dist(a, stat, n, rng)
    return (float(stat(a)), float(np.quantile(dist, alpha / 2)), float(np.quantile(dist, 1 - alpha / 2)))


def bootstrap_diff_ci(a: Sequence[float], b: Sequence[float], stat: Callable = np.median, n: int = 2000,
                      seed: int = 0, alpha: float = 0.05) -> tuple[float, float, float]:
    """Percentile bootstrap of stat(a) - stat(b), resampling each group independently."""
    x, y = _arr(a), _arr(b)
    if x.size == 0 or y.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    dx = _bootstrap_dist(x, stat, n, rng)
    dy = _bootstrap_dist(y, stat, n, rng)
    d = dx - dy
    return (float(stat(x) - stat(y)), float(np.quantile(d, alpha / 2)), float(np.quantile(d, 1 - alpha / 2)))


def _insufficient(method: str, test: str, n: int, why: str) -> StatResult:
    return StatResult(method=method, test=test, n=int(n), supported=False, warnings=[why])


# --------------------------------------------------------------------------------------------
# rates across groups
# --------------------------------------------------------------------------------------------
def chi_square_rates(groups: Sequence[Mapping[str, Any]], *, alpha: float = 0.05, min_cramers_v: float = MIN_CRAMERS_V,
                     min_rate_ratio: float = MIN_RATE_RATIO) -> StatResult:
    """Chi-square test of independence between segment and a boolean outcome.

    groups: [{"segment", "n", "positives"}]. Uses the uncorrected Pearson statistic (no Yates
    correction, also for 2x2) so the statistic and Cramér's V are comparable across table sizes.
    Effect: Cramér's V; rates with Wilson 95% CIs; worst/best rate ratio with a Katz log CI.
    """
    method = "rate_by_segment"
    rows = [{"segment": str(g["segment"]), "n": int(g["n"]), "positives": int(g["positives"])} for g in groups
            if int(g["n"]) > 0]
    N = sum(r["n"] for r in rows)
    P = sum(r["positives"] for r in rows)
    if len(rows) < 2:
        return _insufficient(method, "chi_square", N, "need at least two non-empty segments")
    if P == 0 or P == N:
        return _insufficient(method, "chi_square", N, "outcome is constant across all rows; no rate difference to test")
    for r in rows:
        if r["positives"] < 0 or r["positives"] > r["n"]:
            raise ValueError(f"invalid counts for segment {r['segment']}: positives must be in [0, n]")
    tbl = np.array([[r["positives"], r["n"] - r["positives"]] for r in rows], dtype=float)
    chi2, p, dof, expected = sps.chi2_contingency(tbl, correction=False)
    k = min(tbl.shape) - 1
    v = math.sqrt(chi2 / (N * k)) if k > 0 else float("nan")
    overall = P / N
    out_groups = []
    for r, e in zip(rows, expected, strict=False):
        rate = r["positives"] / r["n"]
        lo, hi = wilson_ci(r["positives"], r["n"])
        resid = (r["positives"] - e[0]) / math.sqrt(e[0] * (1 - r["n"] / N) * (1 - overall)) if e[0] > 0 else None
        out_groups.append({"segment": r["segment"], "n": r["n"], "positives": r["positives"], "rate": _f(rate),
                           "ci_low": _f(lo), "ci_high": _f(hi), "expected_positives": _f(e[0], 3),
                           "adjusted_residual": _f(resid, 3)})
    top = max(out_groups, key=lambda g: (g["rate"], g["n"]))
    base = min(out_groups, key=lambda g: (g["rate"], -g["n"]))
    warns: list[str] = []
    rr = rr_lo = rr_hi = None
    if base["rate"] and base["rate"] > 0:
        rr = top["rate"] / base["rate"]
        a, n1, c, n2 = top["positives"], top["n"], base["positives"], base["n"]
        if a > 0 and c > 0:
            se = math.sqrt(1 / a - 1 / n1 + 1 / c - 1 / n2)
            rr_lo, rr_hi = math.exp(math.log(rr) - Z95 * se), math.exp(math.log(rr) + Z95 * se)
    else:
        warns.append(f"baseline segment '{base['segment']}' has a 0% rate; rate ratio is undefined")
    small = int((expected < 5).sum())
    if small:
        share = small / expected.size
        warns.append(f"{small} of {expected.size} expected cell counts are < 5 ({share:.0%}); "
                     "chi-square approximation may be unreliable" + (" (more than 20% of cells)" if share > 0.2 else ""))
    if (expected < 1).any():
        warns.append("at least one expected cell count is < 1")
    effect_ok = v >= min_cramers_v and (rr is None or rr >= min_rate_ratio)
    supported = _sig(p, alpha) and effect_ok
    hl = {"top_segment": top["segment"], "top_rate": _q(top["rate"]), "top_n": top["n"],
          "baseline_segment": base["segment"], "baseline_rate": _q(base["rate"]), "baseline_n": base["n"],
          "rate_ratio": _f(rr, 3), "rate_ratio_ci": [_f(rr_lo, 3), _f(rr_hi, 3)],
          "rate_difference": _q(top["rate"] - base["rate"]), "overall_rate": _q(overall),
          "rate_ratio_vs_overall": _f(top["rate"] / overall, 3) if overall > 0 else None,
          "cramers_v": _q(v), "p_value": _f(p, 8), "n": N, "n_groups": len(out_groups)}
    return StatResult(method=method, test="chi_square_independence", n=N, statistic=_f(chi2), p_value=_f(p, 12),
                      effect_size=_f(v), effect_label="cramers_v", ci_low=_f(rr_lo), ci_high=_f(rr_hi),
                      groups=out_groups, highlights=hl,
                      assumptions=["independent observations", "expected cell counts >= 5",
                                   "ci_low/ci_high: Katz 95% CI of the worst/best rate ratio"],
                      warnings=warns, supported=supported,
                      details={"dof": int(dof), "alpha": alpha, "thresholds": {"cramers_v": min_cramers_v,
                                                                               "rate_ratio": min_rate_ratio}})


def permutation_chi_square(groups: Sequence[Mapping[str, Any]], *, n_perm: int = 2000, seed: int = 0) -> dict[str, Any]:
    """Monte Carlo permutation p-value for the chi-square statistic with fixed margins.

    Under H0 (outcome independent of segment) shuffling outcome labels across rows is equivalent to
    drawing each segment's positive count from a multivariate hypergeometric distribution, so the
    test needs only the aggregated table.
    """
    n = np.array([int(g["n"]) for g in groups], dtype=np.int64)
    pos = np.array([int(g["positives"]) for g in groups], dtype=np.int64)
    N, P = int(n.sum()), int(pos.sum())
    if len(n) < 2 or P in (0, N):
        return {"p_value": None, "statistic": None, "n_perm": 0}

    def stat(k: np.ndarray) -> np.ndarray:
        e1 = n * P / N
        e0 = n * (N - P) / N
        return (((k - e1) ** 2) / e1 + (((n - k) - e0) ** 2) / e0).sum(axis=-1)

    obs = float(stat(pos))
    rng = np.random.default_rng(seed)
    sims = rng.multivariate_hypergeometric(n, P, size=n_perm)
    null = stat(sims)
    p = (1 + int((null >= obs - 1e-9).sum())) / (n_perm + 1)
    return {"p_value": p, "statistic": obs, "n_perm": n_perm}


def grouped_logistic(groups: Sequence[Mapping[str, Any]], *, baseline: str, alpha: float = 0.05) -> dict[str, Any]:
    """Logistic regression with segment dummies fitted on the aggregated binomial table.

    A binomial GLM on (positives, negatives) per segment is exactly the row-level logistic
    regression with segment dummies, so no row sample is needed. Returns odds ratios vs `baseline`
    (Wald CIs) and the likelihood-ratio test against the intercept-only model. A segment with 0 or
    all positives would make its odds ratio infinite (separation); in that case 0.5 is added to every
    cell (Haldane-Anscombe) and a warning is returned.
    """
    import statsmodels.api as sm

    segs = [str(g["segment"]) for g in groups]
    if baseline not in segs:
        raise ValueError("baseline must be one of the segments")
    pos = np.array([float(g["positives"]) for g in groups])
    neg = np.array([float(g["n"]) - float(g["positives"]) for g in groups])
    warns: list[str] = []
    if (pos == 0).any() or (neg == 0).any():
        pos, neg = pos + 0.5, neg + 0.5
        warns.append("a segment has 0% or 100% positives (separation); applied +0.5 Haldane-Anscombe correction")
    others = [s for s in segs if s != baseline]
    X = np.array([[1.0] + [1.0 if s == o else 0.0 for o in others] for s in segs])
    endog = np.column_stack([pos, neg])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = sm.GLM(endog, X, family=sm.families.Binomial()).fit()
        null = sm.GLM(endog, np.ones((len(segs), 1)), family=sm.families.Binomial()).fit()
    lr = 2 * (res.llf - null.llf)
    lr_p = float(sps.chi2.sf(lr, len(others))) if others else None
    ci = np.asarray(res.conf_int(alpha=alpha))
    ors = {}
    for i, o in enumerate(others, start=1):
        ors[o] = {"odds_ratio": _f(math.exp(res.params[i])), "ci_low": _f(math.exp(ci[i][0])),
                  "ci_high": _f(math.exp(ci[i][1])), "p_value": _f(res.pvalues[i], 12)}
    return {"baseline": baseline, "odds_ratios": ors, "lr_statistic": _f(lr), "lr_p_value": _f(lr_p, 12),
            "warnings": warns}


# --------------------------------------------------------------------------------------------
# numeric comparisons
# --------------------------------------------------------------------------------------------
def _describe(values: np.ndarray) -> dict[str, Any]:
    return {"n": int(values.size), "mean": _f(values.mean()) if values.size else None,
            "median": _f(np.median(values)) if values.size else None,
            "p25": _f(np.quantile(values, 0.25)) if values.size else None,
            "p75": _f(np.quantile(values, 0.75)) if values.size else None,
            "std": _f(values.std(ddof=1)) if values.size > 1 else None}


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def mann_whitney(a: Sequence[float], b: Sequence[float], *, labels: tuple[str, str] = ("a", "b"), alpha: float = 0.05,
                 min_effect: float = MIN_RANK_BISERIAL) -> StatResult:
    """Two-sided Mann-Whitney U (statistic = U of `a`).

    Effect: rank-biserial r = 2*U/(n1*n2) - 1, oriented top-vs-baseline (top = higher median), so
    r > 0 means the top group's values tend to be larger; r = P(top > base) - P(top < base).
    """
    x, y = _arr(a), _arr(b)
    n = x.size + y.size
    if x.size < 2 or y.size < 2:
        return _insufficient("numeric_by_segment", "mann_whitney_u", n, "each group needs at least 2 values")
    if np.ptp(np.concatenate([x, y])) == 0:
        return _insufficient("numeric_by_segment", "mann_whitney_u", n, "all values are identical")
    res = sps.mannwhitneyu(x, y, alternative="two-sided")
    u = float(res.statistic)
    r = 2 * u / (x.size * y.size) - 1
    ga = {"segment": labels[0], **_describe(x)}
    gb = {"segment": labels[1], **_describe(y)}
    top, base = (ga, gb) if (ga["median"], ga["mean"]) >= (gb["median"], gb["mean"]) else (gb, ga)
    if top is gb:  # orient the effect as top vs baseline so a positive r means "top tends larger"
        r = -r
    supported = _sig(res.pvalue, alpha) and abs(r) >= min_effect
    hl = {"top_segment": top["segment"], "top_median": _q(top["median"]), "top_mean": _q(top["mean"]),
          "baseline_segment": base["segment"], "baseline_median": _q(base["median"]), "baseline_mean": _q(base["mean"]),
          "median_ratio": _f(_ratio(top["median"], base["median"]), 3),
          "mean_ratio": _f(_ratio(top["mean"], base["mean"]), 3),
          "median_difference": _q(top["median"] - base["median"]), "rank_biserial": _q(r), "p_value": _f(res.pvalue, 8),
          "n": n}
    return StatResult(method="numeric_by_segment", test="mann_whitney_u", n=n, statistic=_f(u), p_value=_f(res.pvalue, 12),
                      effect_size=_f(r), effect_label="rank_biserial", groups=[ga, gb], highlights=hl,
                      assumptions=["independent observations", "ordinal or continuous outcome",
                                   "tests stochastic dominance, not a difference in medians per se"],
                      supported=supported, details={"alpha": alpha, "threshold_abs_rank_biserial": min_effect})


def kruskal_wallis(groups: Mapping[str, Sequence[float]], *, alpha: float = 0.05, min_effect: float = MIN_EPSILON_SQ) -> StatResult:
    """Kruskal-Wallis H across >= 2 groups. Effect: epsilon² = H / (n - 1).

    Also reports the top-vs-baseline (highest vs lowest median) Mann-Whitney comparison.
    """
    data = {str(k): _arr(v) for k, v in groups.items()}
    data = {k: v for k, v in data.items() if v.size > 0}
    n = sum(v.size for v in data.values())
    if len(data) < 2 or any(v.size < 2 for v in data.values()):
        return _insufficient("numeric_by_segment", "kruskal_wallis_h", n, "need >= 2 groups with >= 2 values each")
    if np.ptp(np.concatenate(list(data.values()))) == 0:
        return _insufficient("numeric_by_segment", "kruskal_wallis_h", n, "all values are identical")
    h, p = sps.kruskal(*data.values())
    eps2 = float(h) / (n - 1)
    desc = [{"segment": k, **_describe(v)} for k, v in data.items()]
    top = max(desc, key=lambda g: (g["median"], g["mean"]))
    base = min(desc, key=lambda g: (g["median"], g["mean"]))
    mw = sps.mannwhitneyu(data[top["segment"]], data[base["segment"]], alternative="two-sided")
    r = 2 * float(mw.statistic) / (data[top["segment"]].size * data[base["segment"]].size) - 1
    supported = _sig(p, alpha) and eps2 >= min_effect
    hl = {"top_segment": top["segment"], "top_median": _q(top["median"]), "top_mean": _q(top["mean"]),
          "baseline_segment": base["segment"], "baseline_median": _q(base["median"]), "baseline_mean": _q(base["mean"]),
          "median_ratio": _f(_ratio(top["median"], base["median"]), 3),
          "mean_ratio": _f(_ratio(top["mean"], base["mean"]), 3),
          "median_difference": _q(top["median"] - base["median"]), "epsilon_squared": _q(eps2),
          "top_vs_baseline_p_value": _f(mw.pvalue, 8), "top_vs_baseline_rank_biserial": _q(r),
          "p_value": _f(p, 8), "n": n, "n_groups": len(desc)}
    return StatResult(method="numeric_by_segment", test="kruskal_wallis_h", n=n, statistic=_f(h), p_value=_f(p, 12),
                      effect_size=_f(eps2), effect_label="epsilon_squared", groups=desc, highlights=hl,
                      assumptions=["independent observations", "ordinal or continuous outcome",
                                   "similar distribution shapes if interpreted as a median difference"],
                      supported=supported, details={"alpha": alpha, "threshold_epsilon_squared": min_effect,
                                                    "dof": len(desc) - 1})


def compare_groups(groups: Mapping[str, Sequence[float]], *, alpha: float = 0.05) -> StatResult:
    """Mann-Whitney for exactly two groups, Kruskal-Wallis otherwise."""
    keys = list(groups)
    if len(keys) == 2:
        return mann_whitney(groups[keys[0]], groups[keys[1]], labels=(str(keys[0]), str(keys[1])), alpha=alpha)
    return kruskal_wallis(groups, alpha=alpha)


def welch_t_test(a: Sequence[float], b: Sequence[float], *, labels: tuple[str, str] = ("a", "b"), alpha: float = 0.05,
                 min_effect: float = MIN_HEDGES_G) -> StatResult:
    """Welch's unequal-variance t-test. Effect: Hedges' g. CI: Welch-Satterthwaite CI of mean(a) - mean(b)."""
    x, y = _arr(a), _arr(b)
    n = x.size + y.size
    if x.size < 2 or y.size < 2:
        return _insufficient("numeric_by_segment", "welch_t", n, "each group needs at least 2 values")
    res = sps.ttest_ind(x, y, equal_var=False)
    vx, vy = x.var(ddof=1), y.var(ddof=1)
    se = math.sqrt(vx / x.size + vy / y.size)
    df = (vx / x.size + vy / y.size) ** 2 / ((vx / x.size) ** 2 / (x.size - 1) + (vy / y.size) ** 2 / (y.size - 1)) if se > 0 else float("nan")
    diff = x.mean() - y.mean()
    tcrit = sps.t.ppf(1 - alpha / 2, df) if math.isfinite(df) else float("nan")
    sp = math.sqrt(((x.size - 1) * vx + (y.size - 1) * vy) / (n - 2)) if n > 2 else float("nan")
    d = diff / sp if sp > 0 else float("nan")
    g = d * (1 - 3 / (4 * n - 9)) if math.isfinite(d) else float("nan")
    supported = _sig(res.pvalue, alpha) and math.isfinite(g) and abs(g) >= min_effect
    ga, gb = {"segment": labels[0], **_describe(x)}, {"segment": labels[1], **_describe(y)}
    top, base = (ga, gb) if diff >= 0 else (gb, ga)
    hl = {"top_segment": top["segment"], "top_mean": _q(top["mean"]), "baseline_segment": base["segment"],
          "baseline_mean": _q(base["mean"]), "mean_difference": _q(abs(diff)),
          "mean_ratio": _f(_ratio(top["mean"], base["mean"]), 3), "hedges_g": _q(g), "p_value": _f(res.pvalue, 8), "n": n}
    return StatResult(method="numeric_by_segment", test="welch_t", n=n, statistic=_f(res.statistic), p_value=_f(res.pvalue, 12),
                      effect_size=_f(g), effect_label="hedges_g", ci_low=_f(diff - tcrit * se), ci_high=_f(diff + tcrit * se),
                      groups=[ga, gb], highlights=hl,
                      assumptions=["independent observations", "approximately normal means (CLT)",
                                   "ci is for mean(a) - mean(b)"],
                      supported=supported, details={"df": _f(df), "alpha": alpha, "threshold_abs_hedges_g": min_effect})


def one_way_anova(groups: Mapping[str, Sequence[float]], *, alpha: float = 0.05, min_effect: float = MIN_ETA_SQ) -> StatResult:
    """One-way ANOVA (F test). Effect: eta² = SS_between / SS_total."""
    data = {str(k): _arr(v) for k, v in groups.items()}
    data = {k: v for k, v in data.items() if v.size > 0}
    n = sum(v.size for v in data.values())
    if len(data) < 2 or any(v.size < 2 for v in data.values()):
        return _insufficient("numeric_by_segment", "one_way_anova", n, "need >= 2 groups with >= 2 values each")
    f, p = sps.f_oneway(*data.values())
    allv = np.concatenate(list(data.values()))
    gm = allv.mean()
    ssb = sum(v.size * (v.mean() - gm) ** 2 for v in data.values())
    sst = ((allv - gm) ** 2).sum()
    eta2 = ssb / sst if sst > 0 else float("nan")
    desc = [{"segment": k, **_describe(v)} for k, v in data.items()]
    top = max(desc, key=lambda g: g["mean"])
    base = min(desc, key=lambda g: g["mean"])
    lev = sps.levene(*data.values())
    warns = [f"Levene test p={lev.pvalue:.3g}: group variances differ; prefer Welch/Kruskal-Wallis"] if lev.pvalue < alpha else []
    supported = _sig(p, alpha) and math.isfinite(eta2) and eta2 >= min_effect
    hl = {"top_segment": top["segment"], "top_mean": _q(top["mean"]), "baseline_segment": base["segment"],
          "baseline_mean": _q(base["mean"]), "mean_ratio": _f(_ratio(top["mean"], base["mean"]), 3),
          "eta_squared": _q(eta2), "p_value": _f(p, 8), "n": n}
    return StatResult(method="numeric_by_segment", test="one_way_anova", n=n, statistic=_f(f), p_value=_f(p, 12),
                      effect_size=_f(eta2), effect_label="eta_squared", groups=desc, highlights=hl,
                      assumptions=["independent observations", "normal residuals", "equal variances"],
                      warnings=warns, supported=supported,
                      details={"alpha": alpha, "threshold_eta_squared": min_effect, "levene_p": _f(lev.pvalue, 8)})


# --------------------------------------------------------------------------------------------
# correlation
# --------------------------------------------------------------------------------------------
def correlation(x: Sequence[float], y: Sequence[float], *, method: str = "spearman", alpha: float = 0.05,
                min_effect: float = MIN_ABS_RHO, x_label: str = "x", y_label: str = "y") -> StatResult:
    """Spearman and Pearson correlation; `method` picks the primary one. Fisher-z 95% CIs."""
    xa = np.asarray(list(x), dtype=float)
    ya = np.asarray(list(y), dtype=float)
    ok = np.isfinite(xa) & np.isfinite(ya)
    xa, ya = xa[ok], ya[ok]
    n = int(xa.size)
    if n < 4:
        return _insufficient("correlation", method, n, "need at least 4 complete pairs")
    if np.ptp(xa) == 0 or np.ptp(ya) == 0:
        return _insufficient("correlation", method, n, "one variable is constant; correlation undefined")
    rho, p_s = sps.spearmanr(xa, ya)
    r, p_p = sps.pearsonr(xa, ya)
    s_lo, s_hi = fisher_z_ci(float(rho), n, spearman=True)
    p_lo, p_hi = fisher_z_ci(float(r), n)
    primary = {"spearman": (float(rho), float(p_s), s_lo, s_hi), "pearson": (float(r), float(p_p), p_lo, p_hi)}
    if method not in primary:
        raise ValueError("method must be spearman or pearson")
    coef, p, lo, hi = primary[method]
    warns = []
    if abs(rho - r) > 0.2:
        warns.append(f"Spearman ({rho:.2f}) and Pearson ({r:.2f}) disagree: relationship is non-linear or outlier-driven")
    supported = _sig(p, alpha) and abs(coef) >= min_effect
    hl = {"x": x_label, "y": y_label, "coefficient": _q(coef), "method": method, "spearman_rho": _q(rho),
          "pearson_r": _q(r), "direction": "positive" if coef > 0 else "negative", "p_value": _f(p, 8),
          "ci": [_q(lo), _q(hi)], "n": n}
    return StatResult(method="correlation", test=method, n=n, statistic=_f(coef), p_value=_f(p, 12), effect_size=_f(coef),
                      effect_label=f"{method}_{'rho' if method == 'spearman' else 'r'}", ci_low=_f(lo), ci_high=_f(hi),
                      highlights=hl, assumptions=["independent pairs", "monotonic (spearman) / linear (pearson) relationship"],
                      warnings=warns, supported=supported,
                      details={"spearman": {"rho": _f(rho), "p_value": _f(p_s, 12), "ci": [_f(s_lo), _f(s_hi)]},
                               "pearson": {"r": _f(r), "p_value": _f(p_p, 12), "ci": [_f(p_lo), _f(p_hi)]},
                               "alpha": alpha, "threshold_abs_coefficient": min_effect})


# --------------------------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------------------------
def _as_matrix(X: Any) -> np.ndarray:
    return np.asarray(X, dtype=float)


def logistic_regression(X: Any, y: Sequence[float], feature_names: Sequence[str], *,
                        feature_groups: Mapping[str, Sequence[int]] | None = None, alpha: float = 0.05,
                        min_odds_ratio: float = MIN_ODDS_RATIO) -> StatResult:
    """Logistic regression (statsmodels Logit, intercept added) with odds ratios and Wald CIs.

    feature_groups: optional {driver: [column indices]} (e.g. one-hot blocks). For each group a
    drop-one likelihood-ratio test is reported in `details["driver_tests"]` and drivers are ranked by
    it; `highlights["top_driver"]` is the driver with the largest LR statistic.

    Separation: if the MLE does not converge, raises, or produces |coef| > 15 / non-finite standard
    errors, the model is refit with L2 regularisation (sklearn, C=1) to report stable odds ratios;
    p-values and CIs are then None and a warning explains why.
    """
    import statsmodels.api as sm

    Xm = _as_matrix(X)
    ya = np.asarray(list(y), dtype=float)
    n = int(ya.size)
    names = list(feature_names)
    if Xm.ndim != 2 or Xm.shape[0] != n or Xm.shape[1] != len(names):
        raise ValueError("X must be n x len(feature_names)")
    if n < 10 or ya.min() == ya.max():
        return _insufficient("driver_model", "logistic_regression", n, "need >= 10 rows and both outcome classes")
    keep = [j for j in range(Xm.shape[1]) if np.ptp(Xm[:, j]) > 0]
    warns: list[str] = []
    dropped = [names[j] for j in range(len(names)) if j not in keep]
    if dropped:
        warns.append(f"constant features dropped: {dropped}")
    Xk = Xm[:, keep]
    kn = [names[j] for j in keep]
    Xc = sm.add_constant(Xk, has_constant="add")

    def fit(mat: np.ndarray):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return sm.Logit(ya, mat).fit(disp=0, maxiter=200)

    separated = False
    res = None
    try:
        res = fit(Xc)
        bse = np.asarray(res.bse)
        if (not res.mle_retvals.get("converged", True) or not np.all(np.isfinite(bse))
                or np.any(np.abs(np.asarray(res.params)[1:]) > 15)):
            separated = True
    except Exception as e:  # noqa: BLE001 - statsmodels raises several types on singular/separated data
        separated = True
        warns.append(f"maximum-likelihood fit failed: {type(e).__name__}")
    groups: list[dict[str, Any]] = []
    details: dict[str, Any] = {"alpha": alpha, "threshold_odds_ratio": min_odds_ratio}
    if separated:
        from sklearn.linear_model import LogisticRegression

        warns.append("perfect or quasi-complete separation detected; odds ratios from L2-regularised fit, "
                     "no p-values or CIs")
        m = LogisticRegression(C=1.0, max_iter=2000).fit(Xk, ya)
        for name, coef in zip(kn, m.coef_[0], strict=False):
            groups.append({"feature": name, "coef": _f(coef), "odds_ratio": _f(math.exp(coef)), "ci_low": None,
                           "ci_high": None, "p_value": None})
        top = max(groups, key=lambda g: abs(g["coef"] or 0)) if groups else None
        hl = {"top_feature": top["feature"] if top else None, "top_odds_ratio": _q(top["odds_ratio"]) if top else None,
              "n": n, "separation": True}
        return StatResult(method="driver_model", test="logistic_regression_l2", n=n, groups=groups, highlights=hl,
                          effect_label="odds_ratio", assumptions=["independent observations", "linear in the log-odds"],
                          warnings=warns, supported=False, details=details)
    assert res is not None
    ci = np.asarray(res.conf_int(alpha=alpha))
    params, pvals = np.asarray(res.params), np.asarray(res.pvalues)
    for i, name in enumerate(kn, start=1):
        groups.append({"feature": name, "coef": _f(params[i]), "odds_ratio": _f(math.exp(params[i])),
                       "ci_low": _f(math.exp(ci[i][0])), "ci_high": _f(math.exp(ci[i][1])), "p_value": _f(pvals[i], 12)})
    llr_p = float(res.llr_pvalue)
    # drop-one LR tests per driver group
    fg = {k: [keep.index(j) for j in v if j in keep] for k, v in (feature_groups or {n_: [i] for i, n_ in enumerate(names)}).items()}
    tests = []
    for drv, idx in fg.items():
        if not idx:
            continue
        rest = [j for j in range(Xk.shape[1]) if j not in idx]
        try:
            red = fit(sm.add_constant(Xk[:, rest], has_constant="add") if rest else np.ones((n, 1)))
            lr = max(0.0, 2 * (res.llf - red.llf))
            tests.append({"driver": drv, "lr_statistic": _f(lr), "df": len(idx), "p_value": _f(sps.chi2.sf(lr, len(idx)), 12)})
        except Exception:  # noqa: BLE001
            tests.append({"driver": drv, "lr_statistic": None, "df": len(idx), "p_value": None})
    tests.sort(key=lambda t: -(t["lr_statistic"] or 0))
    details["driver_tests"] = tests
    details["pseudo_r2"] = _f(res.prsquared)
    details["llr_p_value"] = _f(llr_p, 12)
    strong = [g for g in groups if _sig(g["p_value"], alpha)
              and (g["odds_ratio"] >= min_odds_ratio or g["odds_ratio"] <= 1 / min_odds_ratio)]
    supported = _sig(llr_p, alpha) and bool(strong)
    topf = max(groups, key=lambda g: abs(g["coef"] or 0)) if groups else None
    sig_sorted = sorted(strong, key=lambda g: g["p_value"])
    hl = {"top_driver": tests[0]["driver"] if tests else None,
          "top_driver_lr_p_value": tests[0]["p_value"] if tests else None,
          "strongest_feature": sig_sorted[0]["feature"] if sig_sorted else (topf["feature"] if topf else None),
          "strongest_odds_ratio": _q(sig_sorted[0]["odds_ratio"]) if sig_sorted else None,
          "n_significant_features": len(strong), "pseudo_r2": _q(res.prsquared), "p_value": _f(llr_p, 8), "n": n,
          "outcome_rate": _q(ya.mean())}
    top_or = sig_sorted[0] if sig_sorted else None
    return StatResult(method="driver_model", test="logistic_regression", n=n, statistic=_f(res.llr), p_value=_f(llr_p, 12),
                      effect_size=_f(top_or["odds_ratio"]) if top_or else None, effect_label="odds_ratio",
                      ci_low=top_or["ci_low"] if top_or else None, ci_high=top_or["ci_high"] if top_or else None,
                      groups=groups, highlights=hl,
                      assumptions=["independent observations", "linear in the log-odds", "no strong multicollinearity"],
                      warnings=warns, supported=supported, details=details)


def feature_importance(X: Any, y: Sequence[float], feature_names: Sequence[str], *,
                       feature_groups: Mapping[str, Sequence[int]] | None = None, seed: int = 0,
                       n_estimators: int = 150, n_repeats: int = 10, max_rows: int = 20000,
                       min_importance: float = MIN_PERM_IMPORTANCE) -> StatResult:
    """Random forest + permutation importance on a held-out 30% split (fixed random_state).

    Classification (binary y, metric ROC AUC) or regression (metric R²). Features listed together
    in `feature_groups` (one-hot blocks) are permuted together so each driver gets one importance.
    """
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
    from sklearn.metrics import r2_score, roc_auc_score
    from sklearn.model_selection import train_test_split

    Xm = _as_matrix(X)
    ya = np.asarray(list(y), dtype=float)
    names = list(feature_names)
    n = int(ya.size)
    warns: list[str] = []
    rng = np.random.default_rng(seed)
    if n > max_rows:
        idx = np.sort(rng.choice(n, size=max_rows, replace=False))
        Xm, ya = Xm[idx], ya[idx]
        warns.append(f"random forest fit on a seeded subsample of {max_rows} of {n} rows")
    classes = np.unique(ya)
    is_clf = classes.size <= 2 and set(classes.tolist()) <= {0.0, 1.0}
    if ya.size < 40 or classes.size < 2:
        return _insufficient("driver_model", "random_forest_permutation_importance", n, "need >= 40 rows and a varying outcome")
    strat = ya if is_clf and min((ya == c).sum() for c in classes) >= 2 else None
    Xtr, Xte, ytr, yte = train_test_split(Xm, ya, test_size=0.3, random_state=seed, stratify=strat)
    if is_clf:
        model = RandomForestClassifier(n_estimators=n_estimators, min_samples_leaf=10, random_state=seed, n_jobs=1)
        model.fit(Xtr, ytr)

        def score(mat: np.ndarray) -> float:
            if np.unique(yte).size < 2:
                return float("nan")
            return float(roc_auc_score(yte, model.predict_proba(mat)[:, 1]))
        metric = "roc_auc"
    else:
        model = RandomForestRegressor(n_estimators=n_estimators, min_samples_leaf=10, random_state=seed, n_jobs=1)
        model.fit(Xtr, ytr)

        def score(mat: np.ndarray) -> float:
            return float(r2_score(yte, model.predict(mat)))
        metric = "r2"
    base = score(Xte)
    fg = dict(feature_groups) if feature_groups else {nm: [i] for i, nm in enumerate(names)}
    prng = np.random.default_rng(seed + 1)
    rows = []
    for drv, idx in fg.items():
        drops = []
        for _ in range(n_repeats):
            perm = prng.permutation(Xte.shape[0])
            Xp = Xte.copy()
            Xp[:, list(idx)] = Xte[perm][:, list(idx)]
            drops.append(base - score(Xp))
        d = np.asarray(drops)
        impurity = float(sum(model.feature_importances_[i] for i in idx))
        rows.append({"driver": drv, "importance_mean": _f(d.mean()), "importance_std": _f(d.std(ddof=1) if d.size > 1 else 0.0),
                     "importance_lower": _f(d.mean() - 2 * (d.std(ddof=1) if d.size > 1 else 0.0)),
                     "impurity_importance": _f(impurity)})
    rows.sort(key=lambda r: -(r["importance_mean"] or 0))
    for rank, r in enumerate(rows, start=1):
        r["rank"] = rank
    top = rows[0] if rows else None
    good_model = (base >= MIN_AUC) if is_clf else (base >= MIN_R2)
    supported = bool(top and good_model and (top["importance_lower"] or 0) > min_importance)
    hl = {"top_driver": top["driver"] if top else None, "top_importance": _q(top["importance_mean"]) if top else None,
          "ranking": [r["driver"] for r in rows], f"holdout_{metric}": _q(base), "n": n}
    return StatResult(method="driver_model", test="random_forest_permutation_importance", n=n, statistic=_f(base),
                      effect_size=top["importance_mean"] if top else None, effect_label=f"permutation_importance_{metric}",
                      groups=rows, highlights=hl,
                      assumptions=["importance is predictive, not causal", "correlated drivers share importance"],
                      warnings=warns, supported=supported,
                      details={"metric": metric, "holdout_score": _f(base), "seed": seed, "n_estimators": n_estimators,
                               "n_repeats": n_repeats, "thresholds": {"min_score": MIN_AUC if is_clf else MIN_R2,
                                                                      "min_importance_lower": min_importance}})


# --------------------------------------------------------------------------------------------
# time series
# --------------------------------------------------------------------------------------------
def linear_trend(values: Sequence[float], periods: Sequence[Any] | None = None, *, alpha: float = 0.05,
                 min_pct_change: float = MIN_TREND_PCT) -> StatResult:
    """OLS slope over the period index (0..n-1), plus Mann-Kendall (Kendall tau vs time).

    Effect: fitted % change first->last period = (fit[-1] - fit[0]) / |fit[0]|.
    """
    from statsmodels.stats.stattools import durbin_watson

    y = np.asarray(list(values), dtype=float)
    per = list(periods) if periods is not None else list(range(len(y)))
    ok = np.isfinite(y)
    y = y[ok]
    per = [p for p, k in zip(per, ok, strict=False) if k]
    n = int(y.size)
    if n < 4:
        return _insufficient("trend", "ols_linear_trend", n, "need at least 4 periods")
    t = np.arange(n, dtype=float)
    if np.ptp(y) == 0:
        return StatResult(method="trend", test="ols_linear_trend", n=n, statistic=0.0, p_value=1.0, effect_size=0.0,
                          effect_label="pct_change_fitted", supported=False, warnings=["series is constant"],
                          highlights={"direction": "flat", "pct_change": 0.0, "n_periods": n})
    lr = sps.linregress(t, y)
    fit0, fit1 = lr.intercept, lr.intercept + lr.slope * (n - 1)
    pct_fit = (fit1 - fit0) / abs(fit0) if fit0 != 0 else None
    pct_raw = (y[-1] - y[0]) / abs(y[0]) if y[0] != 0 else None
    tau, tau_p = sps.kendalltau(t, y)
    resid = y - (lr.intercept + lr.slope * t)
    dw = float(durbin_watson(resid))
    warns = []
    if dw < 1.0 or dw > 3.0:
        warns.append(f"Durbin-Watson {dw:.2f}: residuals autocorrelated; OLS p-value is optimistic")
    if n < 8:
        warns.append("fewer than 8 periods; trend estimates are fragile")
    supported = _sig(lr.pvalue, alpha) and pct_fit is not None and abs(pct_fit) >= min_pct_change
    lo = lr.slope - sps.t.ppf(1 - alpha / 2, n - 2) * lr.stderr
    hi = lr.slope + sps.t.ppf(1 - alpha / 2, n - 2) * lr.stderr
    hl = {"direction": "increasing" if lr.slope > 0 else "decreasing", "slope_per_period": _q(lr.slope),
          "pct_change": _q(pct_fit), "pct_change_raw": _q(pct_raw), "first_period": _period_str(per[0]),
          "last_period": _period_str(per[-1]), "first_value": _q(y[0]), "last_value": _q(y[-1]),
          "fitted_first": _q(fit0), "fitted_last": _q(fit1), "kendall_tau": _q(tau), "kendall_p_value": _f(tau_p, 8),
          "r_squared": _q(lr.rvalue ** 2), "p_value": _f(lr.pvalue, 8), "n_periods": n}
    return StatResult(method="trend", test="ols_linear_trend", n=n, statistic=_f(lr.slope), p_value=_f(lr.pvalue, 12),
                      effect_size=_f(pct_fit), effect_label="pct_change_fitted", ci_low=_f(lo), ci_high=_f(hi),
                      groups=[{"period": _period_str(p), "value": _f(v), "fitted": _f(lr.intercept + lr.slope * i)}
                              for i, (p, v) in enumerate(zip(per, y, strict=False))],
                      highlights=hl,
                      assumptions=["equally spaced periods", "independent residuals", "ci is for the slope per period"],
                      warnings=warns, supported=supported,
                      details={"durbin_watson": _f(dw), "alpha": alpha, "threshold_abs_pct_change": min_pct_change,
                               "mann_kendall": {"tau": _f(tau), "p_value": _f(tau_p, 12)}})


def _period_str(p: Any) -> Any:
    if hasattr(p, "isoformat"):
        return p.isoformat()
    if isinstance(p, np.generic):
        return p.item()
    return p


def _best_split(y: np.ndarray, min_size: int) -> tuple[int, float] | None:
    n = y.size
    if n < 2 * min_size:
        return None
    cs, cs2 = np.cumsum(y), np.cumsum(y * y)
    tot, tot2 = cs[-1], cs2[-1]
    best, best_gain = None, 0.0
    sse_all = tot2 - tot * tot / n
    for k in range(min_size, n - min_size + 1):
        l1, l2 = cs[k - 1], cs2[k - 1]
        sse = (l2 - l1 * l1 / k) + ((tot2 - l2) - (tot - l1) ** 2 / (n - k))
        gain = sse_all - sse
        if gain > best_gain + 1e-12:
            best, best_gain = k, gain
    return (best, best_gain) if best is not None else None


def change_point(values: Sequence[float], periods: Sequence[Any] | None = None, *, alpha: float = 0.05, min_size: int = 3,
                 max_points: int = 3, min_shift_pct: float = MIN_SHIFT_PCT) -> StatResult:
    """Binary segmentation for mean shifts (least-squares split = CUSUM maximum).

    A split is accepted when the Welch t-test between the two sides has p < alpha / (#candidate
    splits in that segment) (Bonferroni over positions) and the relative shift in mean is >=
    `min_shift_pct`; accepted segments are split recursively up to `max_points` change points.
    Note: a steady linear trend also produces a split; read together with `linear_trend`.
    """
    y = np.asarray(list(values), dtype=float)
    per = list(periods) if periods is not None else list(range(len(y)))
    n = int(y.size)
    if n < 2 * min_size or not np.all(np.isfinite(y)):
        return _insufficient("trend", "binary_segmentation", n, f"need >= {2 * min_size} finite periods")
    found: list[dict[str, Any]] = []
    stack = [(0, n)]
    while stack and len(found) < max_points:
        s, e = stack.pop(0)
        seg = y[s:e]
        sp = _best_split(seg, min_size)
        if not sp:
            continue
        k = sp[0]
        left, right = seg[:k], seg[k:]
        if np.ptp(seg) == 0:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tt = sps.ttest_ind(left, right, equal_var=False)
        p = float(tt.pvalue) if math.isfinite(tt.pvalue) else (0.0 if left.mean() != right.mean() else 1.0)
        candidates = seg.size - 2 * min_size + 1
        m0, m1 = float(left.mean()), float(right.mean())
        shift = (m1 - m0) / abs(m0) if m0 != 0 else None
        if p < alpha / max(candidates, 1) and (shift is None or abs(shift) >= min_shift_pct):
            idx = s + k
            found.append({"index": idx, "period": _period_str(per[idx]), "before_mean": _f(m0), "after_mean": _f(m1),
                          "shift_pct": _f(shift), "p_value": _f(p, 12)})
            stack += [(s, idx), (idx, e)]
    found.sort(key=lambda c: c["index"])
    strongest = min(found, key=lambda c: c["p_value"]) if found else None
    hl: dict[str, Any] = {"n_change_points": len(found), "n_periods": n}
    if strongest:
        hl.update({"change_period": strongest["period"], "change_index": strongest["index"],
                   "before_mean": _q(strongest["before_mean"]), "after_mean": _q(strongest["after_mean"]),
                   "shift_pct": _q(strongest["shift_pct"])})
    return StatResult(method="trend", test="binary_segmentation", n=n, p_value=strongest["p_value"] if strongest else None,
                      effect_size=strongest["shift_pct"] if strongest else None, effect_label="shift_pct",
                      groups=found, highlights=hl, supported=bool(found),
                      assumptions=["piecewise-constant mean", "independent observations within segments"],
                      details={"alpha": alpha, "min_size": min_size, "threshold_abs_shift_pct": min_shift_pct})


# --------------------------------------------------------------------------------------------
# concentration & anomalies
# --------------------------------------------------------------------------------------------
def gini(values: Sequence[float]) -> float:
    v = np.sort(_arr(values))
    if v.size == 0 or v.sum() == 0:
        return 0.0
    n = v.size
    idx = np.arange(1, n + 1)
    return float((2 * (idx * v).sum()) / (n * v.sum()) - (n + 1) / n)


def pareto_concentration(items: Sequence[Mapping[str, Any]], *, alpha: float = 0.05, top_k: int = 3,
                         min_top_share_ratio: float = MIN_TOP_SHARE_RATIO, min_top20_share: float = MIN_TOP20_SHARE) -> StatResult:
    """Concentration of volume across segments. items: [{"segment", "volume"}].

    Reports top-1 and top-k share, share of the top 20% of segments, segments needed for 80% of
    volume and the Gini coefficient; significance from a chi-square goodness-of-fit vs uniform.
    """
    rows = sorted(({"segment": str(i["segment"]), "volume": float(i["volume"])} for i in items
                   if i.get("volume") is not None and float(i["volume"]) > 0), key=lambda r: (-r["volume"], r["segment"]))
    k = len(rows)
    total = sum(r["volume"] for r in rows)
    if k < 2 or total <= 0:
        return _insufficient("pareto", "concentration", int(total), "need at least two segments with volume")
    cum = 0.0
    for r in rows:
        r["share"] = r["volume"] / total
        cum += r["volume"]
        r["cumulative_share"] = cum / total
    n20 = max(1, math.ceil(0.2 * k))
    top20 = sum(r["share"] for r in rows[:n20])
    kk = min(top_k, k)
    topk = sum(r["share"] for r in rows[:kk])
    n80 = next(i for i, r in enumerate(rows, start=1) if r["cumulative_share"] >= 0.8 - 1e-12)
    g = gini([r["volume"] for r in rows])
    fair = 1 / k
    vols = np.array([r["volume"] for r in rows])
    counts_like = bool(np.allclose(vols, np.round(vols)))
    warns = []
    if counts_like:
        chi2, p = sps.chisquare(vols)
    else:
        chi2, p = None, None
        warns.append("volumes are not counts; goodness-of-fit test skipped, verdict on effect size only")
    ratio = rows[0]["share"] / fair
    effect_ok = ratio >= min_top_share_ratio or top20 >= min_top20_share
    supported = effect_ok and (p is None or _sig(p, alpha))
    out = [{"segment": r["segment"], "volume": _f(r["volume"]), "share": _f(r["share"]),
            "cumulative_share": _f(r["cumulative_share"])} for r in rows]
    hl = {"top_segment": rows[0]["segment"], "top_share": _q(rows[0]["share"]), "top_volume": _f(rows[0]["volume"]),
          "top_share_vs_fair_share": _f(ratio, 3), f"top_{kk}_share": _q(topk), "top_k": kk,
          "top_20pct_segments": n20, "top_20pct_share": _q(top20), "segments_for_80pct": n80, "gini": _q(g),
          "n_segments": k, "total_volume": _f(total), "p_value": _f(p, 8)}
    return StatResult(method="pareto", test="concentration_gof_uniform", n=int(total), statistic=_f(chi2), p_value=_f(p, 12),
                      effect_size=_f(g), effect_label="gini", groups=out, highlights=hl,
                      assumptions=["segments are exhaustive for the filtered population", "GOF null: equal volume per segment"],
                      warnings=warns, supported=supported,
                      details={"alpha": alpha, "thresholds": {"top_share_ratio": min_top_share_ratio,
                                                              "top20_share": min_top20_share}})


def robust_anomalies(values: Sequence[float], labels: Sequence[Any] | None = None, *, z_threshold: float = ROBUST_Z,
                     iqr_k: float = 1.5) -> StatResult:
    """Median/MAD modified z-scores (0.6745*(x-med)/MAD) and Tukey IQR fences."""
    raw = np.asarray(list(values), dtype=float)
    labs = list(labels) if labels is not None else list(range(raw.size))
    ok = np.isfinite(raw)
    x = raw[ok]
    labs = [lb for lb, k in zip(labs, ok, strict=False) if k]
    n = int(x.size)
    if n < 5:
        return _insufficient("anomaly", "robust_z_iqr", n, "need at least 5 values")
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    q1, q3 = np.quantile(x, [0.25, 0.75])
    iqr = q3 - q1
    lo_f, hi_f = q1 - iqr_k * iqr, q3 + iqr_k * iqr
    warns = []
    if mad == 0:
        warns.append("MAD is 0 (more than half the values identical); robust z uses mean absolute deviation")
        meanad = float(np.mean(np.abs(x - med)))
        z = (x - med) / (1.253314 * meanad) if meanad > 0 else np.zeros_like(x)
    else:
        z = 0.6745 * (x - med) / mad
    out = []
    for lb, v, zi in zip(labs, x, z, strict=False):
        iq = bool(v < lo_f or v > hi_f)
        if abs(zi) >= z_threshold or iq:
            out.append({"label": _period_str(lb), "value": _f(v), "robust_z": _f(zi, 3), "iqr_outlier": iq,
                        "z_outlier": bool(abs(zi) >= z_threshold), "direction": "high" if v > med else "low"})
    out.sort(key=lambda r: -abs(r["robust_z"] or 0))
    nz = sum(1 for r in out if r["z_outlier"])
    hl = {"n_anomalies": nz, "n_iqr_outliers": sum(1 for r in out if r["iqr_outlier"]), "median": _q(med), "mad": _q(mad),
          "iqr_low_fence": _q(lo_f), "iqr_high_fence": _q(hi_f), "n": n,
          "top_anomaly": out[0]["label"] if out else None, "top_anomaly_value": out[0]["value"] if out else None}
    return StatResult(method="anomaly", test="robust_z_iqr", n=n, effect_size=_f(max((abs(v) for v in z), default=0)),
                      effect_label="max_abs_robust_z", groups=out, highlights=hl, warnings=warns, supported=nz > 0,
                      assumptions=["roughly symmetric bulk distribution"],
                      details={"z_threshold": z_threshold, "iqr_k": iqr_k})
