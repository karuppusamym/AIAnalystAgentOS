"""P4-06 on the in-memory control plane: idempotency keys (claim, replay, in progress, 409 on a
different body, lease takeover after a crash), the run dispatch outbox (crash before and after
dispatch, orchestrator down, cancellation before dispatch, reconciliation of orphaned runs, the local
orchestrator ignoring a re-delivered start), revision headers and cursor binding."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from analystos.api import http
from analystos.core.errors import IdempotencyConflict, IdempotencyInProgress, InvalidInput, PreconditionFailed, PreconditionRequired
from analystos.core.ids import utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, DispatchOutbox, IdempotencyRecord, RunEvent
from analystos.services import dispatch
from analystos.services import idempotency as idem
from analystos.services import runs as runs_svc


def _key(body: dict, key: str = "k-1") -> idem.Key:
    return idem.Key.of(key, principal="usr_1", workspace_id="ws_1", operation="run.create", request=body)


# ------------------------------------------------------------------------------------ idempotency
def test_no_header_means_no_key_and_bad_keys_are_refused():
    assert idem.Key.of(None, principal="u", workspace_id="w", operation="op", request={}) is None
    with pytest.raises(InvalidInput):
        idem.Key.of("  ", principal="u", workspace_id="w", operation="op", request={})
    with pytest.raises(InvalidInput):
        idem.Key.of("x" * 201, principal="u", workspace_id="w", operation="op", request={})


def test_transactional_claim_then_replay_then_conflict(sqlite_db):
    k = _key({"objective": "a"})
    with session_scope() as s:
        assert idem.begin_in(s, k) is None  # claimed inside the creating transaction
        idem.complete_in(s, k, response={"run_id": "run_1"}, resource_type="analysis_run", resource_id="run_1")
    with session_scope() as s:
        replay = idem.begin_in(s, k)
        assert (replay.resource_id, replay.status_code, replay.response) == ("run_1", 200, {"run_id": "run_1"})
    with session_scope() as s, pytest.raises(IdempotencyConflict) as err:
        idem.begin_in(s, _key({"objective": "b"}))
    assert err.value.http_status == 409 and err.value.code == "idempotency_conflict"
    other_scope = idem.Key.of("k-1", principal="usr_2", workspace_id="ws_1", operation="run.create", request={"objective": "b"})
    with session_scope() as s:
        assert idem.begin_in(s, other_scope) is None  # keys are scoped by principal


def test_a_rolled_back_creation_frees_the_key(sqlite_db):
    k = _key({"objective": "a"})
    with pytest.raises(RuntimeError), session_scope() as s:
        idem.begin_in(s, k)
        raise RuntimeError("creation failed")
    with session_scope() as s:
        assert idem.begin_in(s, k) is None


def test_long_claim_in_progress_release_and_lease_takeover(sqlite_db):
    k = _key({"question": "q"})
    assert idem.claim(k) is None
    with pytest.raises(IdempotencyInProgress) as err:
        idem.claim(k)
    assert err.value.retryable and err.value.http_status == 409
    idem.release(k)  # the work failed: a retry runs it again
    assert idem.claim(k) is None
    with session_scope() as s:  # the first attempt crashed and its lease ran out
        s.scalar(select(IdempotencyRecord)).locked_until = utcnow() - timedelta(seconds=1)
    assert idem.claim(k) is None
    idem.complete(k, response={"turn_id": "t"}, resource_type="ask_turn", resource_id="t")
    assert idem.claim(k).resource_id == "t"


def test_expired_records_are_purged_and_the_key_is_free_again(sqlite_db):
    k = _key({"objective": "a"})
    with session_scope() as s:
        idem.begin_in(s, k)
        idem.complete_in(s, k, response={}, resource_type="analysis_run", resource_id="run_1")
        s.flush()
        s.scalar(select(IdempotencyRecord)).expires_at = utcnow() - timedelta(days=1)
    with session_scope() as s:
        assert idem.begin_in(s, _key({"objective": "different"})) is None  # past retention: not a conflict
    with session_scope() as s:
        s.scalar(select(IdempotencyRecord)).expires_at = utcnow() - timedelta(days=1)
        s.scalar(select(IdempotencyRecord)).status = "completed"
    with session_scope() as s:
        assert idem.purge_expired(s) == 1


# ------------------------------------------------------------------------------------ outbox
def _run(run_id: str = "run_1", *, status: str = "NEW", control: str = "run", age: timedelta | None = None) -> None:
    with session_scope() as s:
        run = AnalysisRun(id=run_id, workspace_id="ws_1", objective="Find the drivers of SLA breaches", status=status,
                          autonomy_level=3, plan={}, plan_version=0, scope={"hash": "h", "assets": []}, instructions=[],
                          constraints={}, control=control, requested_by="usr_1", summary={}, origin={"type": "user"}, capabilities={})
        if age is not None:
            run.created_at = utcnow() - age
        s.add(run)


def _enqueue(run_id: str = "run_1") -> str:
    with session_scope() as s:
        return dispatch.enqueue(s, s.get(AnalysisRun, run_id)).id


def test_orchestrator_down_leaves_the_row_pending_and_the_relay_delivers_later(sqlite_db, monkeypatch):
    _run()
    oid = _enqueue()
    calls: list[str] = []

    def down(run_id):
        calls.append(run_id)
        raise ConnectionError("temporal unreachable")
    monkeypatch.setattr(runs_svc, "start_run", down)
    assert dispatch.dispatch(oid) == "pending"
    with session_scope() as s:
        row = s.get(DispatchOutbox, oid)
        assert row.status == "pending" and row.attempts == 1 and "temporal unreachable" in row.last_error
        assert s.scalar(select(RunEvent).where(RunEvent.type == "run.dispatch_failed")) is not None
    assert dispatch.relay() == {}  # backing off: not due yet
    monkeypatch.setattr(runs_svc, "start_run", lambda run_id: calls.append(run_id) or f"wf-{run_id}")
    assert dispatch.relay(now=utcnow() + timedelta(minutes=10)) == {"dispatched": 1}
    with session_scope() as s:
        row, run = s.get(DispatchOutbox, oid), s.get(AnalysisRun, "run_1")
        assert row.status == "dispatched" and row.workflow_id == "wf-run_1" and run.workflow_id == "wf-run_1"
    assert dispatch.dispatch(oid) == "skipped" and calls == ["run_1", "run_1"]  # a delivered row is never re-sent


def test_crash_before_dispatch_is_relayed_and_crash_after_dispatch_converges(sqlite_db, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(runs_svc, "start_run", lambda run_id: started.append(run_id) or f"analysis-{run_id}")
    _run()
    oid = _enqueue()  # committed with the run; the process died before dispatching
    assert dispatch.relay() == {"dispatched": 1} and started == ["run_1"]
    # Crash after dispatch: the workflow started but the row was never marked; the relay re-sends the same
    # stable workflow id, which the orchestrator treats as "already started".
    with session_scope() as s:
        s.get(DispatchOutbox, oid).status = "pending"
    assert dispatch.relay() == {"dispatched": 1} and started == ["run_1", "run_1"]
    with session_scope() as s:
        assert s.get(AnalysisRun, "run_1").workflow_id == "analysis-run_1"


def test_cancelled_before_dispatch_never_starts_and_finishes_cancelled(sqlite_db, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(runs_svc, "start_run", lambda run_id: started.append(run_id) or "wf")
    _run(control="cancel")
    oid = _enqueue()
    assert dispatch.dispatch(oid) == "cancelled" and started == []
    with session_scope() as s:
        assert s.get(DispatchOutbox, oid).status == "cancelled"
        assert s.get(AnalysisRun, "run_1").status == "CANCELLED"


def test_reconcile_gives_orphaned_new_runs_an_outbox_row(sqlite_db, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(runs_svc, "start_run", lambda run_id: started.append(run_id) or f"wf-{run_id}")
    _run("run_old", age=timedelta(minutes=10))  # created before the outbox, never dispatched
    _run("run_young")  # a creator may still be dispatching it
    _run("run_done", status="COMPLETED", age=timedelta(minutes=10))
    out = dispatch.reconcile()
    assert out == {"enqueued": 1, "dispatched": 1} and started == ["run_old"]
    assert dispatch.reconcile() == {"enqueued": 0}  # idempotent


def test_local_orchestrator_ignores_a_redelivered_start(monkeypatch):
    from analystos.workflows import orchestrator

    gate, calls = threading.Event(), []

    def slow(run_id, **kw):
        calls.append(run_id)
        gate.wait(5)
        return "COMPLETED"
    monkeypatch.setattr(orchestrator, "run_local", slow)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: type("S", (), {"orchestrator": "local"})())
    assert orchestrator.start_run("run_x") == orchestrator.start_run("run_x") == "local-run_x"
    gate.set()
    for _ in range(50):
        if "run_x" not in orchestrator._local_active:
            break
        threading.Event().wait(0.02)
    assert calls == ["run_x"] and "run_x" not in orchestrator._local_active


# ------------------------------------------------------------------------------------ http helpers
def test_if_match_parsing():
    assert http.expected_revision('"3"', required=True) == 3
    assert http.expected_revision('W/"4"', required=True) == 4
    assert http.expected_revision("*", required=True) is None
    assert http.expected_revision(None, required=False) is None
    with pytest.raises(PreconditionRequired) as err:
        http.expected_revision(None, required=True)
    assert err.value.http_status == 428
    with pytest.raises(PreconditionFailed) as err:
        http.expected_revision('"abc"', required=True)
    assert err.value.http_status == 412


def test_cursor_is_bound_to_its_workspace_and_query():
    t = utcnow()
    c = http.encode_cursor(t, "run_1", {"workspace": "ws_1", "list": "runs"})
    assert http.decode_cursor(c, {"workspace": "ws_1", "list": "runs"}) == (t, "run_1")
    with pytest.raises(InvalidInput, match="different workspace"):
        http.decode_cursor(c, {"workspace": "ws_2", "list": "runs"})
    with pytest.raises(InvalidInput, match="not valid"):
        http.decode_cursor("!!!", {"workspace": "ws_1", "list": "runs"})
