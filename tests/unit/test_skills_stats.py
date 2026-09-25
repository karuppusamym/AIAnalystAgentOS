"""Statistical skills vs direct scipy/statsmodels computations and known textbook values."""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats as sps

from analystos.skills import stats as st


# ---------------------------------------------------------------- known values
def test_wilson_known_values():
    lo, hi = st.wilson_ci(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = st.wilson_ci(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-4), pytest.approx(0.7634, abs=1e-4))
    lo, hi = st.wilson_ci(81, 263)
    assert (lo, hi) == (pytest.approx(0.2553, abs=1e-3), pytest.approx(0.3662, abs=1e-3))


def test_chi_square_known_2x2():
    r = st.chi_square_rates([{"segment": "a", "n": 30, "positives": 10}, {"segment": "b", "n": 30, "positives": 20}])
    assert r.statistic == pytest.approx(60 * (10 * 10 - 20 * 20) ** 2 / (30 ** 4), rel=1e-9)  # 6.6667
    assert r.effect_size == pytest.approx(1 / 3, rel=1e-6)
    assert r.p_value == pytest.approx(sps.chi2.sf(20 / 3, 1), rel=1e-6)
    h = r.highlights
    assert h["top_segment"] == "b" and h["baseline_segment"] == "a"
    assert h["top_rate"] == pytest.approx(0.6667, abs=1e-4) and h["rate_ratio"] == pytest.approx(2.0)
    assert h["overall_rate"] == 0.5 and h["rate_ratio_vs_overall"] == pytest.approx(1.333, abs=1e-3)
    assert r.supported is True
    g = {x["segment"]: x for x in r.groups}
    lo, hi = st.wilson_ci(20, 30)
    assert g["b"]["ci_low"] == pytest.approx(lo, abs=1e-6) and g["b"]["ci_high"] == pytest.approx(hi, abs=1e-6)
    # Katz CI of the rate ratio
    se = math.sqrt(1 / 20 - 1 / 30 + 1 / 10 - 1 / 30)
    assert r.ci_low == pytest.approx(math.exp(math.log(2) - 1.959964 * se), rel=1e-4)


def test_chi_square_matches_scipy_kx2_and_warns_small_expected():
    groups = [{"segment": s, "n": n, "positives": p} for s, n, p in [("x", 120, 30), ("y", 80, 10), ("z", 12, 1), ("w", 200, 44)]]
    r = st.chi_square_rates(groups)
    tbl = np.array([[g["positives"], g["n"] - g["positives"]] for g in groups])
    chi2, p, dof, _ = sps.chi2_contingency(tbl, correction=False)
    assert r.statistic == pytest.approx(chi2, rel=1e-6) and r.p_value == pytest.approx(p, rel=1e-6)
    assert r.details["dof"] == dof
    assert r.effect_size == pytest.approx(math.sqrt(chi2 / tbl.sum()), rel=1e-6)
    assert any("expected cell counts are < 5" in w for w in r.warnings)


def test_chi_square_effect_threshold_blocks_tiny_significant_difference():
    # huge n, 10.0% vs 10.5%: significant but rate ratio 1.05 < 1.2 -> not supported
    r = st.chi_square_rates([{"segment": "a", "n": 400000, "positives": 40000}, {"segment": "b", "n": 400000, "positives": 42000}])
    assert r.p_value < 1e-10
    assert r.supported is False


def test_chi_square_degenerate_inputs():
    assert st.chi_square_rates([{"segment": "a", "n": 10, "positives": 1}]).supported is False
    r = st.chi_square_rates([{"segment": "a", "n": 10, "positives": 0}, {"segment": "b", "n": 10, "positives": 0}])
    assert r.supported is False and r.warnings
    r = st.chi_square_rates([{"segment": "a", "n": 100, "positives": 0}, {"segment": "b", "n": 100, "positives": 30}])
    assert r.highlights["rate_ratio"] is None and any("0% rate" in w for w in r.warnings)
    assert r.supported is True  # undefined ratio but clear difference


def test_benjamini_hochberg_known_and_vs_statsmodels():
    assert st.benjamini_hochberg([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert st.benjamini_hochberg([]) == []
    from statsmodels.stats.multitest import multipletests

    p = np.random.default_rng(1).uniform(0, 0.2, size=37)
    assert st.benjamini_hochberg(p) == pytest.approx(list(multipletests(p, method="fdr_bh")[1]), rel=1e-12)
    assert max(st.benjamini_hochberg([0.9, 0.95, 0.99])) <= 1.0


# ---------------------------------------------------------------- numeric comparisons
@pytest.fixture(scope="module")
def two_groups():
    rng = np.random.default_rng(5)
    return rng.lognormal(2.0, 0.7, 400) * 1.5, rng.lognormal(2.0, 0.7, 500)


def test_mann_whitney_vs_scipy(two_groups):
    a, b = two_groups
    r = st.mann_whitney(a, b, labels=("after", "business"))
    ref = sps.mannwhitneyu(a, b, alternative="two-sided")
    assert r.statistic == pytest.approx(ref.statistic) and r.p_value == pytest.approx(ref.pvalue, rel=1e-6)
    assert r.effect_size == pytest.approx(2 * ref.statistic / (400 * 500) - 1, rel=1e-6)
    assert r.highlights["top_segment"] == "after" and r.highlights["median_ratio"] == pytest.approx(
        np.median(a) / np.median(b), abs=1e-3)
    assert r.supported
    # orientation: swapping the arguments keeps the effect positive for the top group
    r2 = st.mann_whitney(b, a, labels=("business", "after"))
    assert r2.effect_size == pytest.approx(r.effect_size, rel=1e-6) and r2.highlights["top_segment"] == "after"


def test_kruskal_vs_scipy_and_epsilon():
    rng = np.random.default_rng(3)
    g = {"a": rng.normal(10, 2, 100), "b": rng.normal(11, 2, 120), "c": rng.normal(13, 2, 90)}
    r = st.kruskal_wallis(g)
    h, p = sps.kruskal(*g.values())
    assert r.statistic == pytest.approx(h) and r.p_value == pytest.approx(p, rel=1e-6)
    assert r.effect_size == pytest.approx(h / (310 - 1))
    assert r.highlights["top_segment"] == "c" and r.highlights["baseline_segment"] == "a"
    assert r.supported
    assert st.compare_groups({"a": [1, 2, 3], "b": [4, 5, 6]}).test == "mann_whitney_u"
    assert st.compare_groups(g).test == "kruskal_wallis_h"


def test_welch_and_anova_vs_scipy(two_groups):
    a, b = two_groups
    r = st.welch_t_test(a, b)
    ref = sps.ttest_ind(a, b, equal_var=False)
    assert r.statistic == pytest.approx(ref.statistic) and r.p_value == pytest.approx(ref.pvalue, rel=1e-6)
    assert r.ci_low < a.mean() - b.mean() < r.ci_high
    rng = np.random.default_rng(9)
    g = {"x": rng.normal(0, 1, 50), "y": rng.normal(0.8, 1, 50), "z": rng.normal(0, 1, 50)}
    an = st.one_way_anova(g)
    f, p = sps.f_oneway(*g.values())
    assert an.statistic == pytest.approx(f) and an.p_value == pytest.approx(p, rel=1e-6)
    assert 0 < an.effect_size < 1 and an.highlights["top_segment"] == "y"


def test_correlation_vs_scipy_with_fisher_ci():
    rng = np.random.default_rng(2)
    x = rng.normal(size=300)
    y = 0.5 * x + rng.normal(size=300)
    r = st.correlation(x, y)
    rho, p = sps.spearmanr(x, y)
    pr, pp = sps.pearsonr(x, y)
    assert r.statistic == pytest.approx(rho) and r.p_value == pytest.approx(p, rel=1e-6)
    assert r.details["pearson"]["r"] == pytest.approx(pr, abs=1e-6)
    z, se = math.atanh(pr), 1 / math.sqrt(297)
    assert r.details["pearson"]["ci"][0] == pytest.approx(math.tanh(z - 1.959964 * se), abs=1e-5)
    assert r.supported and r.highlights["direction"] == "positive"
    assert st.correlation(x, np.ones(300)).supported is False
    assert st.correlation(x, rng.normal(size=300)).supported is False


# ---------------------------------------------------------------- models
def _logit_data(n=3000, seed=4):
    rng = np.random.default_rng(seed)
    x1 = rng.integers(0, 7, n).astype(float)
    x2 = rng.normal(size=n)
    cat = rng.integers(0, 3, n)
    d1, d2 = (cat == 1).astype(float), (cat == 2).astype(float)
    lin = -2.0 + 0.4 * x1 + 0.0 * x2 + 0.6 * d2
    y = (rng.random(n) < 1 / (1 + np.exp(-lin))).astype(float)
    return np.column_stack([x1, x2, d1, d2]), y


def test_logistic_regression_vs_statsmodels():
    import statsmodels.api as sm

    X, y = _logit_data()
    names = ["x1", "x2", "cat=1", "cat=2"]
    r = st.logistic_regression(X, y, names, feature_groups={"x1": [0], "x2": [1], "cat": [2, 3]})
    ref = sm.Logit(y, sm.add_constant(X)).fit(disp=0)
    g = {x["feature"]: x for x in r.groups}
    for i, nm in enumerate(names, start=1):
        assert g[nm]["odds_ratio"] == pytest.approx(math.exp(ref.params[i]), rel=1e-4)
        assert g[nm]["p_value"] == pytest.approx(ref.pvalues[i], rel=1e-3, abs=1e-12)
    assert r.p_value == pytest.approx(ref.llr_pvalue, rel=1e-3, abs=1e-15)
    assert r.highlights["top_driver"] == "x1" and r.supported
    tests = {t["driver"]: t for t in r.details["driver_tests"]}
    assert tests["cat"]["df"] == 2 and tests["x2"]["p_value"] > 0.01


def test_logistic_regression_separation_is_graceful():
    x = np.r_[np.zeros(50), np.ones(50)]
    y = x.copy()
    r = st.logistic_regression(x.reshape(-1, 1), y, ["x"])
    assert r.supported is False
    assert any("separation" in w for w in r.warnings)
    assert r.groups[0]["odds_ratio"] > 1 and r.groups[0]["p_value"] is None


def test_feature_importance_deterministic_and_ranks_true_driver():
    X, y = _logit_data(n=2500)
    a = st.feature_importance(X, y, ["x1", "x2", "cat=1", "cat=2"], feature_groups={"x1": [0], "x2": [1], "cat": [2, 3]})
    b = st.feature_importance(X, y, ["x1", "x2", "cat=1", "cat=2"], feature_groups={"x1": [0], "x2": [1], "cat": [2, 3]})
    assert a.groups == b.groups
    assert a.highlights["top_driver"] == "x1" and a.supported
    assert a.details["metric"] == "roc_auc"


# ---------------------------------------------------------------- time series
def test_linear_trend_vs_linregress_and_kendall():
    rng = np.random.default_rng(8)
    y = 100 + 3 * np.arange(24) + rng.normal(0, 4, 24)
    r = st.linear_trend(y)
    lr = sps.linregress(np.arange(24), y)
    tau, _ = sps.kendalltau(np.arange(24), y)
    assert r.statistic == pytest.approx(lr.slope) and r.p_value == pytest.approx(lr.pvalue, rel=1e-6)
    assert r.highlights["kendall_tau"] == pytest.approx(tau, abs=1e-4)
    fit0, fit1 = lr.intercept, lr.intercept + lr.slope * 23
    assert r.effect_size == pytest.approx((fit1 - fit0) / fit0, rel=1e-5)
    assert r.supported and r.highlights["direction"] == "increasing"
    flat = st.linear_trend(100 + rng.normal(0, 1, 24))
    assert flat.supported is False


def test_change_point_finds_planted_shift():
    rng = np.random.default_rng(11)
    y = np.r_[rng.normal(10, 1, 15), rng.normal(16, 1, 12)]
    periods = [f"2025-{i:02d}" for i in range(27)]
    r = st.change_point(y, periods)
    assert r.supported and r.highlights["change_index"] == 15 and r.highlights["change_period"] == "2025-15"
    assert r.highlights["before_mean"] == pytest.approx(y[:15].mean(), abs=1e-3)
    assert r.highlights["after_mean"] == pytest.approx(y[15:].mean(), abs=1e-3)
    assert st.change_point(rng.normal(10, 1, 30)).supported is False


# ---------------------------------------------------------------- concentration, anomalies, bootstrap
def test_pareto_known_values():
    r = st.pareto_concentration([{"segment": s, "volume": v} for s, v in [("a", 50), ("b", 30), ("c", 10), ("d", 10)]])
    assert r.highlights["top_share"] == 0.5 and r.highlights["top_3_share"] == 0.9
    assert r.effect_size == pytest.approx(0.35)
    assert st.gini([1, 1, 1, 1]) == pytest.approx(0.0)
    assert r.highlights["segments_for_80pct"] == 2
    chi2, p = sps.chisquare([50, 30, 10, 10])
    assert r.p_value == pytest.approx(p, rel=1e-6)
    uniform = st.pareto_concentration([{"segment": str(i), "volume": 100 + i} for i in range(10)])
    assert uniform.supported is False


def test_robust_anomalies():
    vals = [10, 11, 9, 10, 12, 10, 11, 9, 10, 55]
    r = st.robust_anomalies(vals, labels=[f"d{i}" for i in range(10)])
    assert r.supported and r.highlights["top_anomaly"] == "d9"
    med = np.median(vals)
    mad = np.median(np.abs(np.array(vals) - med))
    assert r.groups[0]["robust_z"] == pytest.approx(0.6745 * (55 - med) / mad, abs=1e-3)
    assert st.robust_anomalies([10, 11, 9, 10, 12, 10, 11, 9, 10, 10.5]).supported is False


def test_bootstrap_ci_deterministic_and_covers():
    x = np.random.default_rng(0).normal(5, 2, 500)
    a = st.bootstrap_ci(x, np.median, n=1000, seed=42)
    b = st.bootstrap_ci(x, np.median, n=1000, seed=42)
    assert a == b and a[1] < 5 < a[2]
    est, lo, hi = st.bootstrap_diff_ci(x + 1, x, np.mean, n=500, seed=1)
    assert est == pytest.approx(1.0) and lo <= 1.0 <= hi
    # non-vectorised statistic falls back to a loop
    e2 = st.bootstrap_ci(x[:50], lambda v: float(np.percentile(v, 90)), n=200, seed=3)
    assert e2[1] <= e2[0] <= e2[2]


def test_permutation_and_grouped_logistic():
    groups = [{"segment": "0", "n": 2000, "positives": 240}, {"segment": "3+", "n": 1500, "positives": 540},
              {"segment": "1", "n": 1500, "positives": 190}]
    perm = st.permutation_chi_square(groups, n_perm=500, seed=0)
    assert perm["p_value"] == pytest.approx(1 / 501)
    gl = st.grouped_logistic(groups, baseline="0")
    odds = (540 / 960) / (240 / 1760)
    assert gl["odds_ratios"]["3+"]["odds_ratio"] == pytest.approx(odds, rel=1e-4)
    null = st.permutation_chi_square([{"segment": "a", "n": 500, "positives": 50}, {"segment": "b", "n": 500, "positives": 52}])
    assert null["p_value"] > 0.5
    sep = st.grouped_logistic([{"segment": "a", "n": 100, "positives": 0}, {"segment": "b", "n": 100, "positives": 20}],
                              baseline="a")
    assert sep["warnings"] and sep["odds_ratios"]["b"]["odds_ratio"] > 10
