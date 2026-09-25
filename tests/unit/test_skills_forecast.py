"""Forecasting skills: Holt-Winters intervals, fallbacks on short/constant series, deviation detection."""
from __future__ import annotations

import math

import numpy as np
import pytest

from analystos.skills.forecast import forecast_deviation, forecast_series


def monthly(n: int, start_year: int = 2020) -> list[str]:
    return [f"{start_year + i // 12}-{i % 12 + 1:02d}" for i in range(n)]


def seasonal_series(n: int, seed: int, noise: float = 2.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    return 100 + 1.5 * t + 12 * np.sin(2 * np.pi * t / 12) + rng.normal(0, noise, n)


def test_forecast_trend_seasonal_shape_and_highlights():
    y = seasonal_series(48, seed=1)
    r = forecast_series(list(y), monthly(48), horizon=6)
    assert r.method == "forecast" and r.test == "holt_winters_damped_additive" and r.n == 48
    h = r.highlights
    assert h["method"] == "holt_winters" and h["seasonal_periods"] == 12  # inferred from monthly labels
    assert h["trend_direction"] == "increasing"
    assert h["lower"] < h["next_value"] < h["upper"]
    assert h["next_period"] == "2024-01"
    fc = [g for g in r.groups if g["kind"] == "forecast"]
    assert [g["period"] for g in fc] == ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06"]
    assert all(g["lower"] <= g["forecast"] <= g["upper"] for g in fc)
    widths = [g["upper"] - g["lower"] for g in fc]
    assert widths[-1] >= widths[0]  # uncertainty does not shrink with the horizon
    assert len([g for g in r.groups if g["kind"] == "history"]) == 48
    assert r.statistic == pytest.approx(h["next_value"], abs=1e-3)


def test_forecast_is_deterministic():
    y = list(seasonal_series(40, seed=3))
    a, b = forecast_series(y, horizon=3, seasonal_periods=12), forecast_series(y, horizon=3, seasonal_periods=12)
    assert a.model_dump() == b.model_dump()


def test_interval_covers_held_out_truth_most_of_the_time():
    covered, trials = 0, 30
    for seed in range(trials):
        y = seasonal_series(49, seed=seed)
        r = forecast_series(list(y[:-1]), monthly(48), horizon=1)
        covered += r.highlights["lower"] <= y[-1] <= r.highlights["upper"]
    assert covered / trials >= 0.8  # nominal 95%


def test_seasonality_needs_two_full_seasons():
    y = list(seasonal_series(20, seed=0))
    r = forecast_series(y, horizon=2, seasonal_periods=12)
    assert r.highlights["method"] == "holt_winters" and r.highlights["seasonal_periods"] is None
    assert any("two full seasons" in w for w in r.warnings)


@pytest.mark.parametrize("values,method", [
    ([10.0, 12.0, 11.0, 13.0, 14.0], "drift"),
    ([10.0, 12.0], "naive"),
    ([7.0], "constant"),
    ([5.0] * 30, "constant"),
    ([3.0, 3.0, 3.0], "constant"),
])
def test_short_and_constant_series_are_safe(values, method):
    r = forecast_series(values, horizon=3)
    h = r.highlights
    assert h["method"] == method
    assert h["lower"] <= h["next_value"] <= h["upper"]
    assert all(math.isfinite(g["forecast"]) for g in r.groups if g["kind"] == "forecast")
    if method == "constant":
        assert h["lower"] == h["upper"] == values[-1] and h["trend_direction"] == "flat"


def test_drift_fallback_direction_and_warning():
    r = forecast_series([1, 2, 3, 4, 5], horizon=2)
    assert r.highlights["next_value"] == pytest.approx(6.0) and r.highlights["trend_direction"] == "increasing"
    assert any("fewer than 8" in w for w in r.warnings)


def test_empty_and_nan_series():
    r = forecast_series([])
    assert r.supported is False and r.n == 0
    r = forecast_series([float("nan"), None, 4.0, 5.0, 6.0])  # type: ignore[list-item]
    assert r.n == 3 and r.highlights["method"] == "drift"


def test_daily_period_labels_extend():
    per = [f"2026-03-{d:02d}" for d in range(1, 22)]
    r = forecast_series([float(i % 7) + i for i in range(21)], per, horizon=2)
    assert r.highlights["next_period"] == "2026-03-22"
    assert r.highlights["seasonal_periods"] == 7


def test_deviation_detected_for_planted_spike():
    for seed in range(5):
        y = seasonal_series(48, seed=seed)
        y[-1] += 40  # ~20 noise sd
        r = forecast_deviation(list(y), monthly(48))
        assert r.supported is True, seed
        assert r.highlights["deviation"] and r.highlights["direction"] == "above" and r.statistic > 2
    y = seasonal_series(48, seed=7)
    y[-1] -= 40
    r = forecast_deviation(list(y), monthly(48))
    assert r.supported is True and r.highlights["direction"] == "below"


def test_no_deviation_for_normal_points():
    flagged = sum(bool(forecast_deviation(list(seasonal_series(48, seed=s)), monthly(48)).supported)
                  for s in range(20))
    assert flagged <= 3  # expected false-positive rate ~5%


def test_deviation_holdout_multiple_points():
    y = seasonal_series(48, seed=2)
    y[-3] += 40
    r = forecast_deviation(list(y), monthly(48), holdout=3)
    assert len(r.groups) == 3 and r.groups[0]["outside"] is True
    assert r.highlights["any_holdout_outside"] is True
    assert r.supported is r.groups[-1]["outside"]  # the verdict is about the latest point


def test_deviation_short_and_constant_safe():
    r = forecast_deviation([1.0, 2.0])
    assert r.supported is False and r.warnings
    const = forecast_deviation([5.0] * 12)
    assert const.supported is False
    jump = forecast_deviation([5.0] * 11 + [9.0])
    assert jump.supported is True and any("zero-width" in w for w in jump.warnings)
