from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from analystos.core.errors import InvalidInput
from analystos.services.monitors import _evaluate_metric, _robust_z
from analystos.services.schedules import next_fire, validate


def test_next_fire_respects_timezone():
    after = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)  # Friday
    nxt = next_fire("0 7 * * 1", "America/New_York", after)  # Mondays 07:00 New York
    assert nxt == datetime(2026, 9, 28, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize("kind,cron,tz,config,msg", [
    ("nope", "0 7 * * 1", "UTC", {}, "kind"),
    ("report", "not a cron", "UTC", {}, "cron"),
    ("report", "0 7 * * 1", "Mars/Olympus", {}, "timezone"),
    ("monitor", "*/5 * * * *", "UTC", {}, "15 minutes"),
    ("reanalysis", "0 7 * * 1", "UTC", {"publish": "auto"}, "approval"),
])
def test_schedule_validation(kind, cron, tz, config, msg):
    with pytest.raises(InvalidInput, match=msg):
        validate(kind, cron, tz, config)


def test_robust_z():
    assert _robust_z([10, 11, 9, 10, 10, 11], 30) > 10
    assert _robust_z([1, 2], 3) is None
    assert _robust_z([5, 5, 5, 5], 5) is None


def _mon(kind, **cfg):
    return SimpleNamespace(kind=kind, config=cfg)


SERIES = {"label": "Missed SLA rate", "points": [(f"2026-0{m}-01", v) for m, v in enumerate([0.14, 0.15, 0.14, 0.15, 0.14, 0.15, 0.14, 0.15, 0.31], start=1)]}


def test_drift_detects_spike_and_reports_numbers():
    r = _evaluate_metric(_mon("metric_drift", lookback=8, z_threshold=3), SERIES)
    assert r["alert"] and r["direction"] == "increase" and r["z"] > 3
    assert "0.31" in r["message"] and "robust z" in r["message"]


def test_drift_quiet_series():
    quiet = {"label": "x", "points": SERIES["points"][:-1] + [("2026-09-01", 0.145)]}
    assert not _evaluate_metric(_mon("metric_drift", lookback=8, z_threshold=3), quiet)["alert"]


def test_threshold():
    assert _evaluate_metric(_mon("metric_threshold", op=">", value=0.2), SERIES)["alert"]
    assert not _evaluate_metric(_mon("metric_threshold", op=">", value=0.5), SERIES)["alert"]


def test_change_point_recent_shift():
    pts = [(f"w{i:02d}", 100.0 + (i % 3)) for i in range(20)] + [(f"w{i:02d}", 150.0 + (i % 3)) for i in range(20, 24)]
    r = _evaluate_metric(_mon("change_point", recent_periods=6), {"label": "Volume", "points": pts})
    assert r["alert"] and r["change_period"] == "w20"


def test_rule_based_redirect_parsing():
    from analystos.services.runs import parse_redirect_rules

    vocab = {("s.incident", "category"): ["network", "software", "inquiry", "hardware"],
             ("s.incident", "contact_type"): ["email", "phone", "self-service"]}
    f = parse_redirect_rules("Exclude inquiry-category incidents; they are requests.", vocab)
    assert f == [{"asset": "s.incident", "column": "category", "op": "!=", "value": "inquiry"}]
    f = parse_redirect_rules("Focus only on software and hardware incidents raised by phone", vocab)
    assert {"asset": "s.incident", "column": "category", "op": "in", "value": ["software", "hardware"]} in f
    assert {"asset": "s.incident", "column": "contact_type", "op": "=", "value": "phone"} in f
    assert parse_redirect_rules("Tell me more about the network", vocab) == []  # no verb -> no filter


def test_forecast_deviation_monitor_follows_trend_and_flags_break():
    # A steady upward trend is expected, not an anomaly (the median-drift check would flag it).
    trend = [(f"2025-{i // 4 + 1:02d}-{(i % 4) * 7 + 1:02d}", 100 + 5 * i + (i % 3)) for i in range(24)]
    quiet = _evaluate_metric(_mon("forecast_deviation", z=2.5), {"label": "Volume", "points": trend})
    assert not quiet["alert"] and quiet["expected"] is not None
    broken = trend[:-1] + [(trend[-1][0], 40)]
    r = _evaluate_metric(_mon("forecast_deviation", z=2.5), {"label": "Volume", "points": broken})
    assert r["alert"] and r["direction"] == "below" and r["pct_change"] < -0.5 and "expected" in r["message"]


def test_schedule_accepts_crawl_kind():
    validate("crawl", "0 3 * * *", "UTC", {"mode": "incremental"})
