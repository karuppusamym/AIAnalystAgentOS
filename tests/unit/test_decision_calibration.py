"""P4-T09: Brier/ECE per purpose x backend from labelled outcomes, and the automatic, recorded,
reversible downgrade (jev -> rules) when a backend is badly calibrated. SQLite control plane."""
from __future__ import annotations

from datetime import timedelta

import pytest
from tests.fakes import FakeTransport

from analystos.contracts.platform import DecisionSettings, PlatformSettings
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AuditEvent, DecisionCalibration, DecisionOutcome, DecisionRecord, User
from analystos.decisions import Question, calibration, store
from analystos.decisions import breaker as breaker_mod
from analystos.decisions.service import DecisionService
from analystos.decisions.store import DbDecisionStore
from analystos.llm.cache import ResponseCache
from analystos.llm.router import ModelRouter


@pytest.fixture
def db(sqlite_db):
    from analystos.db import models

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t]
                                                    for t in ("decision", "decision_outcome", "decision_calibration")])
    store.invalidate()
    breaker_mod.reset_all()
    with session_scope() as s:
        s.add(User(id="usr_admin", email="admin@x", name="A", password_hash="x", is_admin=True, attributes={}))
    yield sqlite_db
    store.invalidate()


def test_brier_and_ece_on_known_values():
    perfect = [({"yes": 1.0, "no": 0.0}, "yes"), ({"yes": 0.0, "no": 1.0}, "no")]
    assert calibration.brier(perfect) == 0.0 and calibration.ece(perfect) == 0.0
    wrong = [({"yes": 0.9, "no": 0.1}, "no")] * 4
    assert calibration.brier(wrong) == pytest.approx(0.81)  # (p - y)^2 for a yes/no decision
    assert calibration.ece(wrong) == pytest.approx(0.9)  # 90% confident, 0% accurate
    multi = [({"a": 0.5, "b": 0.5}, "a")]
    assert calibration.brier(multi) == pytest.approx(0.25)  # 1/2 * (0.25 + 0.25)
    assert calibration.brier([]) is None and calibration.distribution({}, "x") == {}


def _decisions(purpose: str, backend: str, p_yes: float, labels: list[str], *, created_at=None) -> None:
    with session_scope() as s:
        for label in labels:
            d = DecisionRecord(id=new_id("dec"), purpose=purpose, authority="escalate_only", backend=backend, inputs_hash="h",
                               subject=f"alert:{new_id('k')}", options={}, answer="warning",
                               probabilities={"yes": p_yes, "no": 1 - p_yes}, latency_ms=1, cost_usd=0.0, attempts=[], enforced=[])
            if created_at is not None:
                d.created_at = created_at
            s.add(d)
            s.flush()
            s.add(DecisionOutcome(decision_id=d.id, purpose=purpose, backend=backend, label=label, source="alert.dismiss"))


def _settings(**kw) -> DecisionSettings:
    return DecisionSettings(**{"calibration_min_outcomes": 20, **kw})


def test_badly_calibrated_backend_is_downgraded_recorded_used_and_reversible(db):
    # JEV said "material" at p=0.9 on 25 alerts users dismissed; the rules were right.
    _decisions("alert_triage", "jev", 0.9, ["no"] * 25)
    _decisions("alert_triage", "rules", 0.0, ["no"] * 25)
    with session_scope() as s:
        result = calibration.run_calibration(s, actor="test", settings=_settings())
    rows = {(r["purpose"], r["backend"]): r for r in result["results"]}
    assert rows[("alert_triage", "jev")]["action"] == "downgraded" and rows[("alert_triage", "jev")]["brier"] == pytest.approx(0.81)
    assert rows[("alert_triage", "rules")]["downgraded"] is False and rows[("alert_triage", "rules")]["brier"] == 0.0
    assert result["downgraded"] == ["alert_triage:jev"]
    assert DbDecisionStore().downgraded() == {("alert_triage", "jev")}
    with session_scope() as s:
        assert s.query(AuditEvent).filter(AuditEvent.action == "decision.backend_downgraded").count() == 1

    # The service now skips JEV for alert_triage (visibly), and still uses it elsewhere.
    t = FakeTransport(decide=lambda p: {"answers": {"p": {"noul": 0.99}}})
    platform = PlatformSettings()
    r = ModelRouter(transport=t, api_key_lookup=lambda _e: "sk-test-0000000000000000", max_retries=0,
                    settings_provider=lambda: platform, cache=ResponseCache(None))
    svc = DecisionService(r, store=DbDecisionStore())
    q = Question.escalation("material?", levels=["info", "warning", "critical"], baseline="warning", escalate_to="critical",
                            escalate_at=0.8)
    d = svc.decide("alert_triage", {"signal": "s"}, q, facts={"points": 12})
    assert d.value == "warning" and d.backend == "rules" and t.decide_calls == []
    assert d.attempts[0]["outcome"] == "downgraded" and "downgraded" in d.fallback_reason
    with session_scope() as s:
        assert s.get(DecisionRecord, d.id).fallback_reason == d.fallback_reason  # the decision row records it
    stop = svc.decide("stop_check", {"objective": "o"}, Question.probability("?"), facts={"round": 1, "supported": 4})
    assert stop.backend == "jev" and len(t.decide_calls) == 1

    # A second nightly run with the same evidence keeps it down without a second downgrade event.
    with session_scope() as s:
        again = calibration.run_calibration(s, actor="test", settings=_settings())
    assert {(r["backend"], r["action"]) for r in again["results"]} == {("jev", "evaluated"), ("rules", "evaluated")}
    assert DbDecisionStore().downgraded() == {("alert_triage", "jev")}

    # Reversible: an administrator restores it; the old evidence no longer counts.
    with session_scope() as s:
        calibration.set_backend_state(s, s.get(User, "usr_admin"), "alert_triage", "jev", downgraded=False, note="new JEV version")
    assert DbDecisionStore().downgraded() == set()
    with session_scope() as s:
        after = calibration.run_calibration(s, actor="test", settings=_settings(), now=utcnow() + timedelta(seconds=1))
    assert not [r for r in after["results"] if r["backend"] == "jev" and r["n"] > 0]
    assert DbDecisionStore().downgraded() == set()
    with session_scope() as s:
        report = calibration.report(s)
    assert report["downgraded"] == [] and report["last_run_at"]
    triage = next(p for p in report["purposes"] if p["purpose"] == "alert_triage")
    assert triage["effective"] == ["rules", "jev"] and report["outcomes"]["alert_triage"] == 50


def test_well_calibrated_backend_is_restored_automatically(db):
    _decisions("feedback_classification", "jev", 0.9, ["no"] * 20)
    with session_scope() as s:
        calibration.run_calibration(s, actor="test", settings=_settings())
    assert DbDecisionStore().downgraded() == {("feedback_classification", "jev")}
    with session_scope() as s:  # the backend (e.g. a new JEV version, run in shadow) is now right
        s.query(DecisionOutcome).delete()
        s.query(DecisionRecord).delete()
    _decisions("feedback_classification", "jev", 0.95, ["yes"] * 20)
    with session_scope() as s:
        result = calibration.run_calibration(s, actor="test", settings=_settings())
    assert [r["action"] for r in result["results"]] == ["restored"] and DbDecisionStore().downgraded() == set()


def test_guards_min_outcomes_pinned_rules_and_auto_downgrade_off(db):
    _decisions("alert_triage", "jev", 0.9, ["no"] * 5)
    _decisions("stop_check", "rules", 1.0, ["no"] * 25)
    with session_scope() as s:
        few = calibration.run_calibration(s, actor="test", settings=_settings(), dry_run=True)
    notes = {(r["purpose"], r["backend"]): r["note"] for r in few["results"]}
    assert notes[("alert_triage", "jev")] == "insufficient outcomes"
    assert notes[("stop_check", "rules")] == "rules is the last resort"
    with session_scope() as s:
        assert s.query(DecisionCalibration).count() == 0  # dry run writes nothing
    _decisions("alert_triage", "jev", 0.9, ["no"] * 20)
    with session_scope() as s:
        pinned = calibration.run_calibration(s, actor="test", settings=_settings(pinned=["alert_triage:jev"]))
        off = calibration.run_calibration(s, actor="test", settings=_settings(auto_downgrade=False))
    assert not pinned["downgraded"] and not off["downgraded"]
    assert DbDecisionStore().downgraded() == set()


def test_old_outcomes_fall_out_of_the_window(db):
    _decisions("alert_triage", "jev", 0.9, ["no"] * 25)
    with session_scope() as s:
        s.query(DecisionOutcome).update({DecisionOutcome.created_at: utcnow() - timedelta(days=90)})
    with session_scope() as s:
        result = calibration.run_calibration(s, actor="test", settings=_settings(calibration_window_days=30))
    assert result["results"] == [] and DbDecisionStore().downgraded() == set()


def test_signals_label_the_decisions_about_their_subject(db):
    with session_scope() as s:
        for purpose in ("rev_second_opinion", "hypothesis_priority"):
            s.add(DecisionRecord(id=new_id("dec"), purpose=purpose, authority="escalate_only", backend="jev", inputs_hash="h",
                                 subject="insight:ins_1", options={}, answer="none", probabilities={"yes": 0.8, "no": 0.2},
                                 latency_ms=1, cost_usd=0.0, attempts=[], enforced=[]))
    with session_scope() as s:
        assert calibration.record_signal(s, "finding.accept", "insight:ins_1", user_id="usr_admin") == 1
        assert calibration.record_signal(s, "finding.reject", "insight:other", user_id="usr_admin") == 0
    with session_scope() as s:
        out = s.query(DecisionOutcome).one()
        assert (out.purpose, out.label, out.source) == ("rev_second_opinion", "yes", "finding.accept")
    with pytest.raises(Exception, match="unknown outcome signal"), session_scope() as s:
        calibration.record_signal(s, "finding.love", "insight:ins_1", user_id=None)
