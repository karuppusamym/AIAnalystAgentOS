"""P7-01 follow-ups (ADR-0020 decision 5): monitors that compare with a verified baseline read its verification
state and refuse (or, when configured, relabel) a VOID one; re-verification is an explicit action that writes a
new record by the subject's own path and never edits the old one. SQLite control plane, no services."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.unit.test_verification_p701 import EXTRA, WS, _insight

from analystos.core.errors import Conflict, InvalidInput
from analystos.db.base import session_scope
from analystos.db.models import VerificationRecord, WorkspaceMember
from analystos.evidence import verification as V


@pytest.fixture
def db(sqlite_db, monkeypatch):
    from analystos.db import models
    from analystos.services import monitors as mon
    from analystos.services import platform_settings

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in EXTRA
                                                    if t in models.Base.metadata.tables])
    monkeypatch.setattr(mon, "_triage", lambda monitor, workspace, result: ("warning", None))
    monkeypatch.setattr(platform_settings, "get", lambda: SimpleNamespace(
        monitors=SimpleNamespace(auto_investigation_enabled=False, triage_escalate_probability=0.8)))
    return sqlite_db


def _setup_monitor(config: dict, *, kind: str = "metric_threshold") -> tuple[str, str]:
    from analystos.db.models import Monitor

    with session_scope() as s:
        ins, rec = _insight(s)
        s.add(Monitor(id="mon_b", workspace_id=WS, name="missed SLA", kind=kind,
                      config={"sql_expression": "AVG(x)", "op": ">", "value": 0.5, "grain": "week",
                              "baseline": {"subject_type": "insight", "subject_id": ins.id}, **config},
                      enabled=True, created_by="u_owner", last_result={"value": 0.4, "message": "last good reading"}))
        return ins.id, rec.id


def _void(rec_id: str) -> None:
    with session_scope() as s:
        dep = next(d for d in s.get(VerificationRecord, rec_id).dependencies
                   if d["kind"] == "query")
        assert V.void_dependents(s, "query", dep["ref"], "v-new", "step H-1 edited", event="step.edited") == [rec_id]


def _series(monkeypatch, calls: list | None = None):
    from analystos.services import monitors as mon

    def fake(s, owner, monitor):
        if calls is not None:
            calls.append(monitor.id)
        return {"label": "Missed SLA", "grain": "week", "points": [("2026-09-14", 0.4), ("2026-09-21", 0.7)],
                "query_id": "q", "excluded_future_rows": 0, "dropped_incomplete_period": None}

    monkeypatch.setattr(mon, "metric_series", fake)


# ------------------------------------------------------------------------------------ monitors read the state
def test_a_monitor_reports_its_live_baseline_state(db, monkeypatch):
    from analystos.services import monitors as mon

    _setup_monitor({})
    _series(monkeypatch)
    out = mon.evaluate_monitor("mon_b")
    assert out["alert"] is True and out["baseline"]["void"] is False
    assert out["baseline"]["subjects"][0]["badge"] == "verified" and "baseline_void" not in out


def test_a_void_baseline_is_refused_and_the_last_reading_is_kept(db, monkeypatch):
    from analystos.db.models import Alert, Monitor
    from analystos.services import monitors as mon

    _, rec_id = _setup_monitor({})
    _void(rec_id)
    calls: list = []
    _series(monkeypatch, calls)
    out = mon.evaluate_monitor("mon_b")
    assert calls == []  # nothing was compared with a void verdict: no query ran
    assert out["alert"] is False and out["state"] == "baseline_void" and out["alert_id"] is None
    assert "query: step H-1 edited" in out["message"] and rec_id in out["message"]
    with session_scope() as s:
        m = s.get(Monitor, "mon_b")
        assert m.state == "baseline_void" and m.last_result["value"] == 0.4  # the last real reading stays
        assert m.last_result["baseline"]["void_record_ids"] == [rec_id]
        assert s.query(Alert).count() == 0


def test_a_relabelling_monitor_evaluates_and_labels_every_reading(db, monkeypatch):
    from analystos.services import monitors as mon

    _, rec_id = _setup_monitor({"on_void_baseline": "relabel"})
    _void(rec_id)
    _series(monkeypatch)
    out = mon.evaluate_monitor("mon_b")
    assert out["alert"] is True and out["baseline_void"] is True
    assert out["message"].startswith("[baseline void: query: step H-1 edited]")


def test_a_baseline_must_be_a_recorded_verdict(db):
    from analystos.db.models import User
    from analystos.services import monitors as mon

    with session_scope() as s:
        _insight(s)
        user = s.get(User, "u_owner")
        s.add(WorkspaceMember(workspace_id=WS, user_id="u_owner", role="owner"))
        s.flush()
        base = {"metric": "missed_sla_rate", "op": ">", "value": 0.5}
        with pytest.raises(InvalidInput, match="no verification record"):
            mon.create_monitor(s, user, WS, name="m", kind="metric_threshold",
                               config={**base, "baseline": {"subject_type": "insight", "subject_id": "ins_nope"}})
        with pytest.raises(InvalidInput, match="on_void_baseline"):
            mon.create_monitor(s, user, WS, name="m", kind="metric_threshold", config={**base, "on_void_baseline": "hide"})
        m = mon.create_monitor(s, user, WS, name="m", kind="metric_threshold",
                               config={**base, "baseline": {"subject_type": "insight", "subject_id": "ins_1"}})
        assert m.config["baseline"]["subject_id"] == "ins_1"


# ------------------------------------------------------------------------------------ explicit re-verification
@pytest.fixture
def reverify_world(db, monkeypatch):
    from analystos.evidence import reverify as R
    from analystos.governance import policy

    monkeypatch.setattr(policy, "require_role", lambda *a, **k: "owner")
    started: list[dict] = []

    def fake_create_run(user, workspace_id, **kw):
        started.append({"workspace_id": workspace_id, **kw})
        return SimpleNamespace(id="run_re1")

    monkeypatch.setattr("analystos.services.runs.create_run", fake_create_run)
    return SimpleNamespace(R=R, started=started, user=SimpleNamespace(id="u_owner"))


def _snapshot(rec_id: str) -> dict:
    from analystos.db.models import VerificationRecord

    with session_scope() as s:
        r = s.get(VerificationRecord, rec_id)
        return {c: getattr(r, c) for c in ("state", "verdict", "fingerprint", "dependencies", "void_kind", "void_reason",
                                           "void_detail", "superseded_by", "checks")}


def test_reverifying_a_void_finding_replays_its_frozen_spec_and_leaves_the_old_record(reverify_world):
    w = reverify_world
    with session_scope() as s:
        ins, rec = _insight(s)
        ins_id, rec_id = ins.id, rec.id
    _void(rec_id)
    before = _snapshot(rec_id)
    out = w.R.reverify(w.user, rec_id)
    assert out["status"] == "started" and out["run_id"] == "run_re1" and out["previous"]["state"] == "VOID"
    call = w.started[0]
    assert call["origin"]["type"] == "reverify" and call["origin"]["record_id"] == rec_id
    assert call["origin"]["subject_id"] == ins_id and call["origin"]["replay"] is True
    [analysis] = call["pins"]["analyses"]
    assert analysis["spec"]["method"] == "rate_by_segment" and len(analysis["spec_hash"]) == 64
    assert _snapshot(rec_id) == before  # never edited


def test_a_current_verdict_or_an_older_record_is_not_reverified(reverify_world):
    w = reverify_world
    with session_scope() as s:
        ins, rec = _insight(s)
        rec_id = rec.id
    with pytest.raises(Conflict, match="nothing to re-verify"):
        w.R.reverify(w.user, rec_id)
    with session_scope() as s:
        newer = V.record_verdict(s, workspace_id=WS, run_id=None, subject_type="insight", subject_id=ins.id,
                                 verdict="verified", checks=[], verifier="rev.v2", dependencies=[])
        newer_id = newer.id
    with pytest.raises(Conflict, match=f"latest: {newer_id}"):
        w.R.reverify(w.user, rec_id)
    assert w.started == []


def test_an_active_record_whose_dependency_moved_silently_is_voided_first(reverify_world):
    from tests.unit.test_verification_p701 import SPEC

    from analystos.db.models import Hypothesis

    w = reverify_world
    with session_scope() as s:
        _, rec = _insight(s)
        rec_id = rec.id
        s.get(Hypothesis, "hyp_1").spec = {**SPEC, "filters": [{"column": "channel", "op": "=", "value": "web"}]}
    out = w.R.reverify(w.user, rec_id)
    assert out["status"] == "started"
    after = _snapshot(rec_id)
    assert after["state"] == "VOID" and after["void_kind"] == "query"
    assert after["void_detail"]["event"] == "verification.reverify_requested"


def test_steps_and_experiments_reverify_through_their_own_paths(reverify_world, monkeypatch):
    w = reverify_world
    with session_scope() as s:
        rec = V.record_verdict(s, workspace_id=WS, run_id=None, subject_type="step", subject_id="stp_1", verdict="verified",
                               checks=[], verifier="selfcheck.v1", dependencies=[V.Dependency("query", "step:stp_1", "v1")])
        rec_id = rec.id
    _void(rec_id)
    seen = []
    monkeypatch.setattr("analystos.services.steps.rerun", lambda user, step_id: seen.append(step_id) or {
        "step": {"id": step_id, "version": 2, "verification_record": {"record_id": "ver_new", "state": "ACTIVE"}},
        "rerun": [{"id": "stp_2"}]})
    out = w.R.reverify(w.user, rec_id)
    assert seen == ["stp_1"] and out["status"] == "reverified" and out["new_record"]["record_id"] == "ver_new"
    assert out["rerun"] == ["stp_2"] and _snapshot(rec_id)["state"] == "VOID"
    with session_scope() as s:
        other = V.record_verdict(s, workspace_id=WS, run_id=None, subject_type="chart", subject_id="c1", verdict="verified",
                                 checks=[], verifier="x", dependencies=[V.Dependency("query", "chart:c1", "v1")])
        other_id = other.id
    with pytest.raises(InvalidInput, match="re-verifies"):
        w.R.reverify(w.user, other_id)
