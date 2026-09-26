"""P4-01 (SEC-002, UI-003): workspace-bound child lookups and SSE streams that lose their caller.

`load_in_workspace` answers every "not yours" case with the same 404 (one or two memberships alike);
an open run-event or Ask stream is re-authorized before each payload and periodically while idle,
and ends with one terminal `expired` / `revoked` frame and nothing after it."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from analystos.api.deps import StreamAuth, stream_guard
from analystos.core.errors import Forbidden, NotFound
from analystos.events import stream as stream_mod
from analystos.events.stream import RunEventHub, StreamGuard, run_event_stream


class FakeHub(RunEventHub):
    def __init__(self):
        super().__init__()
        self.connected = True  # nudges drive the stream; the fallback poll is long

    def _ensure_listener(self) -> None:
        return None


async def _never_disconnected() -> bool:
    return False


@pytest.fixture
def world(sqlite_db):
    """ws_a and ws_b; `single` is an analyst of ws_a only, `dual` an analyst of both; one run in each."""
    from analystos.db.models import AnalysisRun, User, Workspace, WorkspaceMember

    with sqlite_db() as s:
        for uid in ("single", "dual"):
            s.add(User(id=uid, email=f"{uid}@x", name=uid, password_hash="x", active=True))
        for ws in ("ws_a", "ws_b"):
            s.add(Workspace(id=ws, name=ws, created_by="dual"))
            s.add(AnalysisRun(id=f"run_{ws[-1]}", workspace_id=ws, objective="o", status="RUNNING", plan={}, plan_version=1,
                              scope={}, instructions=[], constraints={}, requested_by="dual", summary={}, origin={"type": "user"}))
        s.add(WorkspaceMember(workspace_id="ws_a", user_id="single", role="analyst"))
        s.add(WorkspaceMember(workspace_id="ws_a", user_id="dual", role="analyst"))
        s.add(WorkspaceMember(workspace_id="ws_b", user_id="dual", role="analyst"))
        s.commit()
    return sqlite_db


def _user(factory, uid: str):
    from analystos.db.models import User

    with factory() as s:
        user = s.get(User, uid)
        s.expunge(user)
    return user


def _load(factory, uid: str, run_id: str, workspace_id: str | None, minimum: str = "viewer"):
    from analystos.db.models import AnalysisRun, User
    from analystos.governance.policy import load_in_workspace

    with factory() as s:
        return load_in_workspace(s, AnalysisRun, run_id, workspace_id, user=s.get(User, uid), minimum=minimum, label="run")


def _message(fn) -> str:
    with pytest.raises(NotFound) as err:
        fn()
    return err.value.message


# ------------------------------------------------------------------------------------ load_in_workspace
def test_path_workspace_must_own_the_child_even_for_a_member_of_both(world):
    assert _load(world, "dual", "run_a", "ws_a").id == "run_a"
    assert _load(world, "dual", "run_b", "ws_b").id == "run_b"
    assert _message(lambda: _load(world, "dual", "run_b", "ws_a")) == "run not found"  # dual: URL ws_a, run of ws_b
    assert _message(lambda: _load(world, "dual", "run_a", "ws_b")) == "run not found"


def test_one_membership_sees_the_same_404_for_foreign_and_unknown_ids(world):
    unknown = _message(lambda: _load(world, "single", "run_nope", None))
    assert _message(lambda: _load(world, "single", "run_b", None)) == unknown == "run not found"  # by id alone
    assert _message(lambda: _load(world, "single", "run_b", "ws_a")) == unknown  # URL ws_a, run of ws_b
    assert _message(lambda: _load(world, "single", "run_b", "ws_b")) == unknown  # URL ws_b: not a member
    assert _message(lambda: _load(world, "single", "run_a", "ws_nope")) == unknown


def test_role_and_deleted_workspace(world):
    from analystos.core.ids import utcnow
    from analystos.db.models import Workspace

    with pytest.raises(Forbidden):
        _load(world, "single", "run_a", "ws_a", minimum="owner")  # a member below the minimum learns only that
    with world() as s:
        s.get(Workspace, "ws_a").deleted_at = utcnow()
        s.commit()
    assert _message(lambda: _load(world, "dual", "run_a", "ws_a")) == "run not found"


# ------------------------------------------------------------------------------------ run event stream
def _emit(factory, run_id: str, n: int) -> None:
    from analystos.db.models import RunEvent

    with factory() as s:
        s.add(RunEvent(workspace_id=f"ws_{run_id[-1]}", run_id=run_id, type="task.updated", payload={"n": n}))
        s.commit()


def _run_stream(factory, uid: str, run_id: str, workspace_id: str, *, expires_at: float | None = None, interval: float = 5.0):
    from analystos.services.runs import get_run_for

    def load(session, user):
        return get_run_for(session, user, run_id, workspace_id=workspace_id)

    guard = stream_guard(StreamAuth(_user(factory, uid), expires_at), load)
    guard._interval = interval
    hub = FakeHub()
    return hub, run_event_stream(run_id, 0, is_disconnected=_never_disconnected, hub=hub, guard=guard)


async def _next(agen, timeout: float = 3.0) -> str | None:
    try:
        return await asyncio.wait_for(agen.__anext__(), timeout)
    except StopAsyncIteration:
        return None


def _revoke_membership(factory, uid: str, ws: str) -> None:
    from sqlalchemy import delete

    from analystos.db.models import WorkspaceMember

    with factory() as s:
        s.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == ws, WorkspaceMember.user_id == uid))
        s.commit()


def test_membership_revoked_mid_stream_sends_no_further_event(world):
    async def main():
        hub, agen = _run_stream(world, "dual", "run_b", "ws_b")
        _emit(world, "run_b", 1)
        first = await _next(agen)
        _revoke_membership(world, "dual", "ws_b")
        _emit(world, "run_b", 2)  # committed after the revocation: must never reach the caller
        hub.nudge("run_b")
        rest = [await _next(agen), await _next(agen)]
        return first, rest

    first, rest = asyncio.run(main())
    assert first.startswith("id: 1\nevent: task.updated")
    assert rest[0] == 'event: revoked\ndata: {"reason": "access_revoked"}\n\n' and rest[1] is None
    assert '"n": 2' not in (rest[0] or "")


@pytest.mark.parametrize("change", ["deactivate", "delete_workspace", "delete_run"])
def test_other_revocations_end_the_stream(world, change):
    from analystos.core.ids import utcnow
    from analystos.db.models import AnalysisRun, User, Workspace

    async def main():
        hub, agen = _run_stream(world, "dual", "run_b", "ws_b")
        _emit(world, "run_b", 1)
        await _next(agen)
        with world() as s:
            if change == "deactivate":
                s.get(User, "dual").active = False
            elif change == "delete_workspace":
                s.get(Workspace, "ws_b").deleted_at = utcnow()
            else:
                s.delete(s.get(AnalysisRun, "run_b"))
            s.commit()
        _emit(world, "run_b", 2)
        hub.nudge("run_b")
        return [await _next(agen), await _next(agen)]

    frames = asyncio.run(main())
    reason = "user_inactive" if change == "deactivate" else "access_revoked"
    assert frames == [f'event: revoked\ndata: {{"reason": "{reason}"}}\n\n', None]


def test_role_lowered_below_the_stream_minimum_ends_it(world):
    """A stream that needs more than viewer (here: analyst) ends when the role drops."""
    from sqlalchemy import update

    from analystos.db.models import WorkspaceMember
    from analystos.services.runs import get_run_for

    async def main():
        guard = stream_guard(StreamAuth(_user(world, "dual"), None),
                             lambda s, u: get_run_for(s, u, "run_b", "analyst", workspace_id="ws_b"))
        hub = FakeHub()
        agen = run_event_stream("run_b", 0, is_disconnected=_never_disconnected, hub=hub, guard=guard)
        _emit(world, "run_b", 1)
        await _next(agen)
        with world() as s:
            s.execute(update(WorkspaceMember).where(WorkspaceMember.user_id == "dual", WorkspaceMember.workspace_id == "ws_b")
                      .values(role="viewer"))
            s.commit()
        _emit(world, "run_b", 2)
        hub.nudge("run_b")
        return [await _next(agen), await _next(agen)]

    assert asyncio.run(main()) == ['event: revoked\ndata: {"reason": "access_revoked"}\n\n', None]


def test_idle_stream_is_rechecked_periodically(world):
    """No event and no nudge: the periodic check alone ends a revoked stream."""
    async def main():
        _hub, agen = _run_stream(world, "dual", "run_b", "ws_b", interval=0.1)
        pending = asyncio.ensure_future(_next(agen))
        await asyncio.sleep(0.15)
        _revoke_membership(world, "dual", "ws_b")
        started = time.monotonic()
        frame = await pending
        return frame, time.monotonic() - started, await _next(agen)

    frame, waited, after = asyncio.run(main())
    assert frame.startswith("event: revoked") and after is None
    assert waited < 1.0


def test_token_expiry_mid_stream_ends_with_expired_and_no_event(world):
    async def main():
        hub, agen = _run_stream(world, "dual", "run_b", "ws_b", expires_at=time.time() + 0.4)
        _emit(world, "run_b", 1)
        first = await _next(agen)
        await asyncio.sleep(0.5)
        _emit(world, "run_b", 2)  # emitted after the token expired
        hub.nudge("run_b")
        return first, await _next(agen), await _next(agen)

    first, terminal, after = asyncio.run(main())
    assert first.startswith("id: 1\n")
    assert terminal == 'event: expired\ndata: {"reason": "token_expired"}\n\n' and after is None


def test_token_expiry_ends_an_idle_stream_on_time(world):
    async def main():
        _hub, agen = _run_stream(world, "dual", "run_b", "ws_b", expires_at=time.time() + 0.3)
        started = time.monotonic()
        return await _next(agen), time.monotonic() - started, await _next(agen)

    frame, waited, after = asyncio.run(main())
    assert frame.startswith("event: expired") and after is None
    assert 0.2 < waited < 1.5  # woke for the expiry, not the 5 s fallback poll


def test_guard_checks_before_every_payload_and_only_periodically_otherwise():
    calls: list[int] = []

    def check():
        calls.append(1)
        return None

    async def main():
        guard = StreamGuard(check, None, interval=60)
        assert await guard.verdict(payload=False) is None and calls == []  # idle, interval not reached
        assert await guard.verdict(payload=True) is None and calls == [1]
        assert await guard.verdict(payload=True) is None and calls == [1, 1]

    asyncio.run(main())


# ------------------------------------------------------------------------------------ Ask stream
def _ask_with_gate(release: threading.Event):
    def fake_ask(user, thread_id, question, parameters, *, on_stage):
        on_stage({"key": "scope", "text": "Checking what you are allowed to see"})
        release.wait(3)
        on_stage({"key": "sql", "text": "Writing the query"})
        return {"id": "askt_1", "status": "answered", "answer": "secret number"}
    return fake_ask


def test_ask_stream_revoked_mid_turn_sends_no_stage_or_answer_after():
    from analystos.services import ask as ask_svc

    state = {"revoked": False}
    release = threading.Event()
    guard = StreamGuard(lambda: "access_revoked" if state["revoked"] else None, None)

    async def main():
        agen = ask_svc.stream_turn(SimpleNamespace(id="u"), "ask_1", "q", ask=_ask_with_gate(release), guard=guard)
        first = await _next(agen)
        state["revoked"] = True
        release.set()
        frames = []
        while (frame := await _next(agen)) is not None:
            frames.append(frame)
        return first, frames

    first, frames = asyncio.run(main())
    assert first.startswith("event: stage")
    assert frames == ['event: revoked\ndata: {"reason": "access_revoked"}\n\n']


def test_ask_stream_token_expiry_mid_turn():
    from analystos.services import ask as ask_svc

    release = threading.Event()
    guard = StreamGuard(lambda: None, time.time() + 0.3)

    async def main():
        agen = ask_svc.stream_turn(SimpleNamespace(id="u"), "ask_1", "q", ask=_ask_with_gate(release), guard=guard)
        first = await _next(agen)
        second = await _next(agen)  # the ask is still blocked: the guard's own timer ends the stream
        release.set()
        return first, second, await _next(agen)

    first, second, after = asyncio.run(main())
    assert first.startswith("event: stage")
    assert second == 'event: expired\ndata: {"reason": "token_expired"}\n\n' and after is None


def test_ask_stream_with_a_valid_guard_is_unchanged():
    from analystos.services import ask as ask_svc

    release = threading.Event()
    release.set()
    guard = StreamGuard(lambda: None, time.time() + 60)

    async def main():
        return [f async for f in ask_svc.stream_turn(SimpleNamespace(id="u"), "ask_1", "q", ask=_ask_with_gate(release),
                                                     guard=guard)]

    events = [f.split("\n", 1)[0] for f in asyncio.run(main())]
    assert events == ["event: stage", "event: stage", "event: turn", "event: end"]


def test_guard_check_runs_off_the_loop(world, monkeypatch):
    """The re-check opens its own short session in a worker thread, never on the event loop."""
    threads: list[int] = []
    real = stream_mod.run_blocking

    async def spy(fn, *args):
        def traced(*a):
            threads.append(threading.get_ident())
            return fn(*a)
        return await real(traced, *args)

    monkeypatch.setattr(stream_mod, "run_blocking", spy)

    async def main():
        _hub, agen = _run_stream(world, "dual", "run_b", "ws_b")
        _emit(world, "run_b", 1)
        frame = await _next(agen)
        await agen.aclose()
        return threading.get_ident(), frame

    loop_thread, frame = asyncio.run(main())
    assert frame.startswith("id: 1\n") and len(threads) >= 2  # the batch read and the guard's check
    assert loop_thread not in threads
