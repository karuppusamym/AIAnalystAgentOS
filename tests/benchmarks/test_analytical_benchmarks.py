"""Analytical-correctness benchmarks: calibration (false-positive rate under the null), power on
planted effects, and interval coverage, via seeded Monte Carlo. Fast enough for the default run.

These guard the *verdict* layer: a change to a threshold or estimator that makes the platform
claim effects that are not there (or miss obvious ones) fails here.
"""
from __future__ import annotations

import numpy as np
import pytest

from analystos.skills import stats as st

REPS = 200
# Monte Carlo tolerance for a rate of alpha=0.05 over REPS runs: alpha + ~2.5 standard errors.
FPR_MAX = 0.08


def _rate_groups(rng, rates, n):
    return [{"segment": str(i), "n": n, "positives": int(rng.binomial(n, r))} for i, r in enumerate(rates)]


def test_chi_square_null_calibration_and_power():
    rng = np.random.default_rng(100)
    null = [st.chi_square_rates(_rate_groups(rng, [0.2] * 4, 500)) for _ in range(REPS)]
    sig_rate = np.mean([r.p_value < 0.05 for r in null])
    assert 0.02 <= sig_rate <= 0.09  # test is calibrated at alpha
    assert np.mean([bool(r.supported) for r in null]) <= FPR_MAX  # verdict never exceeds the test's alpha
    alt = [st.chi_square_rates(_rate_groups(rng, [0.1, 0.1, 0.1, 0.2], 500)) for _ in range(REPS)]
    assert np.mean([bool(r.supported) for r in alt]) >= 0.95
    assert np.mean([r.highlights["top_segment"] == "3" for r in alt]) >= 0.95


def test_effect_thresholds_suppress_trivial_differences_at_scale():
    """At operational row counts tiny real differences are 'significant'; the verdict must not be."""
    rng = np.random.default_rng(107)
    rs = [st.chi_square_rates(_rate_groups(rng, [0.200, 0.205, 0.210], 60000)) for _ in range(50)]
    assert np.mean([r.p_value < 0.05 for r in rs]) >= 0.5
    assert not any(r.supported for r in rs)
    ms = [st.mann_whitney(rng.normal(100, 20, 20000), rng.normal(100.6, 20, 20000)) for _ in range(20)]
    assert np.mean([m.p_value < 0.05 for m in ms]) >= 0.5 and not any(m.supported for m in ms)
    cs = []
    for _ in range(20):
        x = rng.normal(size=20000)
        cs.append(st.correlation(x, 0.04 * x + rng.normal(size=20000)))
    assert np.mean([c.p_value < 0.05 for c in cs]) >= 0.5 and not any(c.supported for c in cs)


def test_wilson_coverage():
    rng = np.random.default_rng(101)
    for p, n in ((0.1, 50), (0.5, 30), (0.02, 400)):
        ks = rng.binomial(n, p, size=2000)
        cover = np.mean([lo <= p <= hi for lo, hi in (st.wilson_ci(k, n) for k in ks)])
        assert 0.92 <= cover <= 0.985, (p, n, cover)


def test_benjamini_hochberg_controls_fdr():
    rng = np.random.default_rng(102)
    fdrs = []
    for _ in range(50):
        p_null = rng.uniform(size=900)
        p_alt = 2 * (1 - __import__("scipy").stats.norm.cdf(rng.normal(3.5, 1, size=100)))
        adj = np.array(st.benjamini_hochberg(np.r_[p_null, p_alt]))
        rej = adj < 0.05
        fdrs.append(rej[:900].sum() / max(rej.sum(), 1))
    assert np.mean(fdrs) <= 0.06


def test_numeric_comparison_calibration_and_power():
    rng = np.random.default_rng(103)
    null = [st.mann_whitney(rng.lognormal(2, 0.8, 300), rng.lognormal(2, 0.8, 300)) for _ in range(REPS)]
    assert np.mean([bool(r.supported) for r in null]) <= FPR_MAX
    alt = [st.mann_whitney(rng.lognormal(2, 0.8, 300) * 1.6, rng.lognormal(2, 0.8, 300)) for _ in range(REPS)]
    assert np.mean([bool(r.supported) for r in alt]) >= 0.95
    ratios = [r.highlights["median_ratio"] for r in alt]
    assert 1.45 <= float(np.median(ratios)) <= 1.75  # quoted number is unbiased for the planted 1.6x
    kw_null = [st.kruskal_wallis({g: rng.normal(0, 1, 150) for g in "abcd"}) for _ in range(100)]
    assert np.mean([bool(r.supported) for r in kw_null]) <= 0.05


def test_correlation_calibration_and_power():
    rng = np.random.default_rng(104)
    null = [st.correlation(rng.normal(size=400), rng.normal(size=400)) for _ in range(REPS)]
    assert np.mean([bool(r.supported) for r in null]) <= FPR_MAX
    alt = []
    for _ in range(REPS):
        x = rng.normal(size=400)
        alt.append(st.correlation(x, 0.3 * x + rng.normal(size=400)))
    assert np.mean([bool(r.supported) for r in alt]) >= 0.95
    cover = np.mean([r.details["pearson"]["ci"][0] <= 0.3 / np.sqrt(1.09) <= r.details["pearson"]["ci"][1] for r in alt])
    assert 0.9 <= cover <= 0.99


def test_trend_calibration_and_power():
    rng = np.random.default_rng(105)
    null = [st.linear_trend(100 + rng.normal(0, 10, 18)) for _ in range(REPS)]
    assert np.mean([bool(r.supported) for r in null]) <= FPR_MAX
    alt = [st.linear_trend(100 + 2.5 * np.arange(18) + rng.normal(0, 10, 18)) for _ in range(REPS)]
    assert np.mean([bool(r.supported) for r in alt]) >= 0.9
    cp_null = [st.change_point(100 + rng.normal(0, 10, 24)) for _ in range(100)]
    assert np.mean([bool(r.supported) for r in cp_null]) <= 0.05
    cp_alt = [st.change_point(np.r_[100 + rng.normal(0, 5, 12), 130 + rng.normal(0, 5, 12)]) for _ in range(100)]
    hits = [r.highlights.get("change_index") == 12 for r in cp_alt]
    assert np.mean(hits) >= 0.9


def test_pareto_calibration_and_power():
    rng = np.random.default_rng(106)
    k = 20
    null = [st.pareto_concentration([{"segment": str(i), "volume": v} for i, v in enumerate(rng.multinomial(600, [1 / k] * k))])
            for _ in range(REPS)]
    assert np.mean([bool(r.supported) for r in null]) <= 0.02
    p = np.r_[0.35, np.full(k - 1, 0.65 / (k - 1))]
    alt = [st.pareto_concentration([{"segment": str(i), "volume": v} for i, v in enumerate(rng.multinomial(600, p))])
           for _ in range(REPS)]
    assert np.mean([bool(r.supported) for r in alt]) >= 0.99
    assert abs(np.mean([r.highlights["top_share"] for r in alt]) - 0.35) < 0.01


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_logistic_driver_recovery(seed):
    rng = np.random.default_rng(seed)
    n = 3000
    x1 = rng.integers(0, 7, n).astype(float)
    x2 = rng.normal(size=n)
    y = (rng.random(n) < np.where(x1 >= 3, 0.36, 0.12)).astype(float)
    r = st.logistic_regression(np.column_stack([x1, x2]), y, ["reassign", "noise"])
    assert r.supported and r.highlights["top_driver"] == "reassign"
    null = st.logistic_regression(np.column_stack([x2, rng.normal(size=n)]), (rng.random(n) < 0.2).astype(float), ["a", "b"])
    assert not null.supported
