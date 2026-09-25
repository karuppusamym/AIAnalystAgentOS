"""P4-C08: the Phase-3 evidence showed the same threshold condition alerting three times (one monitor
per script run, each with its own dedupe key) and once at x100 scale (a legacy percent KPI whose
definition multiplied by 100). One condition and period now yield one alert, re-creating a monitor
is idempotent, and percent KPIs are fractions end to end."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from analystos.core.errors import InvalidInput
from analystos.db.base import session_scope
from analystos.db.models import Alert, Artifact, Monitor, Notification, User, Workspace
from analystos.services import monitors as mon

THRESHOLD = {"metric": "critical_incident_rate", "grain": "month", "op": ">", "value": 0.0}


@pytest.fixture
def world(sqlite_db, monkeypatch):
    with session_scope() as s:
        s.add(User(id="usr_1", email="a@x", name="A", password_hash="x", attributes={}))
        s.add(Workspace(id="ws_1", name="w", objective="find the drivers of SLA breaches", created_by="usr_1", settings={}))
    triage_calls: list[str] = []

    def triage(monitor, workspace, result):
        triage_calls.append(monitor.id)
        return "critical", {"p_material": 0.85, "model": "typesafe/jev-test"}

    monkeypatch.setattr(mon, "_triage", triage)
    monkeypatch.setattr(mon, "require_role", lambda *a, **k: "owner")
    from analystos.services import platform_settings

    monkeypatch.setattr(platform_settings, "get", lambda: SimpleNamespace(
        monitors=SimpleNamespace(auto_investigation_enabled=False, triage_escalate_probability=0.8)))
    series: dict[str, list] = {"points": [("2026-07-01", 0.0391), ("2026-08-01", 0.04192)]}
    monkeypatch.setattr(mon, "metric_series", lambda s, owner, monitor: {
        "label": "Critical Incident Rate", "grain": "month", "points": series["points"], "query_id": "q",
        "excluded_future_rows": 0, "dropped_incomplete_period": None})
    return SimpleNamespace(triage_calls=triage_calls, series=series)


def _monitor(mid: str, name: str, config: dict = THRESHOLD) -> str:
    with session_scope() as s:
        s.add(Monitor(id=mid, workspace_id="ws_1", name=name, kind="metric_threshold", config=dict(config), enabled=True,
                      auto_investigate=True, created_by="usr_1", last_result={}))
    return mid


def _alerts() -> list[Alert]:
    with session_scope() as s:
        return list(s.scalars(select(Alert).order_by(Alert.created_at)))


def test_same_condition_from_two_monitors_raises_one_alert(world):
    first = mon.evaluate_monitor(_monitor("mon_a", "critical_incident_rate above target"))
    second = mon.evaluate_monitor(_monitor("mon_b", "critical_incident_rate above target (rerun)"))
    assert first["alert"] and second["alert"]
    assert first["alert_id"] == second["alert_id"]
    assert len(_alerts()) == 1
    assert world.triage_calls == ["mon_a"]  # no second JEV triage, notification or investigation
    with session_scope() as s:
        assert len(list(s.scalars(select(Notification).where(Notification.kind == "alert")))) == 1
    assert mon.evaluate_monitor("mon_a")["alert_id"] == first["alert_id"]  # re-evaluation: same alert


def test_new_period_is_a_new_alert_and_recovery_resolves_the_shared_alert(world):
    first = mon.evaluate_monitor(_monitor("mon_a", "rate"))
    world.series["points"] = [*world.series["points"], ("2026-09-01", 0.05)]
    later = mon.evaluate_monitor(_monitor("mon_b", "rate again"))
    assert later["alert_id"] != first["alert_id"]
    world.series["points"] = [("2026-08-01", 0.0), ("2026-09-01", 0.0)]
    assert not mon.evaluate_monitor("mon_b")["alert"]
    assert {a.status for a in _alerts()} == {"resolved"}  # including the alert first raised by mon_a


def test_different_threshold_is_a_different_condition(world):
    a = mon.evaluate_monitor(_monitor("mon_a", "rate"))
    b = mon.evaluate_monitor(_monitor("mon_b", "rate", {**THRESHOLD, "value": 0.01}))
    assert a["alert_id"] != b["alert_id"]


def test_legacy_per_monitor_dedupe_key_is_still_honoured(world):
    _monitor("mon_a", "rate")
    with session_scope() as s:
        s.add(Alert(id="alr_old", workspace_id="ws_1", monitor_id="mon_a", severity="critical", title="t", message="m",
                    data={}, dedupe_key="mon_a:2026-08-01", status="open"))
    assert mon.evaluate_monitor("mon_a")["alert_id"] == "alr_old"


def test_create_monitor_is_idempotent_per_condition(world):
    with session_scope() as s:
        user = s.get(User, "usr_1")
        m1 = mon.create_monitor(s, user, "ws_1", name="rate above target", kind="metric_threshold", config=dict(THRESHOLD),
                                auto_investigate=True)
        s.flush()
        m2 = mon.create_monitor(s, user, "ws_1", name="rate above target", kind="metric_threshold", config=dict(THRESHOLD),
                                auto_investigate=True)
        m3 = mon.create_monitor(s, user, "ws_1", name="rate above 5%", kind="metric_threshold",
                                config={**THRESHOLD, "value": 0.05}, auto_investigate=True)
        assert m1.id == m2.id and m3.id != m1.id


# ------------------------------------------------------------------------------------ x100 scale
def test_percent_series_outside_unit_interval_is_rejected():
    mon.check_scale("Rate", "percent", [("2026-07-01", 0.0391), ("2026-08-01", 1.0)])
    mon.check_scale("Volume", "number", [("2026-08-01", 407.0)])
    with pytest.raises(InvalidInput, match="outside \\[0, 1\\]"):
        mon.check_scale("Critical Incident Rate", "percent", [("2026-08-01", 4.192)])


def _metric(aid: str, run_id: str, expression: str, value: float) -> None:
    with session_scope() as s:
        s.add(Artifact(id=aid, workspace_id="ws_1", run_id=run_id, type="metric", name="critical_incident_rate",
                       content={"name": "critical_incident_rate", "display_name": "Critical Incident Rate",
                                "sql_expression": expression, "format": "percent", "validation": {"value": value}},
                       content_hash=aid))


def test_monitor_never_resolves_a_legacy_x100_percent_definition(world):
    with session_scope() as s:
        s.add(Artifact(id="art_ds", workspace_id="ws_1", run_id="run_new", type="dataset", name="ds",
                       content={"sql": "select 1", "raw_time_column": "opened_at"}, content_hash="d"))
    _metric("art_fraction", "run_old", 'AVG(CASE WHEN "is_critical" THEN 1.0 ELSE 0.0 END)', 0.0419)
    _metric("art_x100", "run_new", 'AVG(CASE WHEN "is_critical" THEN 100.0 ELSE 0.0 END)', 4.19)
    with session_scope() as s:
        m = s.get(Monitor, _monitor("mon_a", "rate"))
        ds, expression, label, fmt = mon._dataset_and_metric(s, m)
    assert "1.0" in expression and fmt == "percent" and label == "Critical Incident Rate"
    with session_scope() as s:
        s.delete(s.get(Artifact, "art_fraction"))
    with session_scope() as s, pytest.raises(Exception, match="legacy x100"):
        mon._dataset_and_metric(s, s.get(Monitor, "mon_a"))
