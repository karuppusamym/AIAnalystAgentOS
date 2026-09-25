"""P4-C04: the SSE endpoint never runs blocking database code on the event loop, wakes on a
Redis nudge instead of a busy poll, resumes from Last-Event-ID, and nudges only after commit."""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from types import SimpleNamespace

import pytest

from analystos.events import stream as stream_mod
from analystos.events.stream import Batch, RunEventHub, run_event_stream


class FakeHub(RunEventHub):
    def __init__(self, connected: bool = True):
        super().__init__()
        self.connected = connected

    def _ensure_listener(self) -> None:  # no Redis in unit tests
        return None


def _event(i: int) -> dict:
    return {"id": i, "type": "task.updated", "payload": {"n": i}, "actor": None, "created_at": None}


async def _never_disconnected() -> bool:
    return False


class GuardedDb:
    """A `session_scope` stand-in that records which thread opened each session."""

    def __init__(self, statuses: list[str], events: list[list]):
        self.threads: list[int] = []
        self.statuses, self.events, self.after_ids = statuses, events, []

    @contextlib.contextmanager
    def __call__(self):
        self.threads.append(threading.get_ident())
        db = self

        class Session:
            def scalar(self, stmt):
                return db.statuses.pop(0) if len(db.statuses) > 1 else db.statuses[0]

            def execute(self, stmt):
                db.after_ids.append(stmt.compile().params.get("id_1"))
                batch = db.events.pop(0) if db.events else []
                return [(i, "t", {}, None, None) for i in batch]
        yield Session()


async def _collect(agen, until: str = "event: end", timeout: float = 5.0) -> list[str]:
    frames: list[str] = []

    async def go():
        async for frame in agen:
            frames.append(frame)
            if frame.startswith(until):
                return
    await asyncio.wait_for(go(), timeout)
    return frames


def test_stream_reads_the_database_only_in_worker_threads(monkeypatch):
    db = GuardedDb(statuses=["RUNNING", "RUNNING", "COMPLETED"], events=[[4, 5], [], []])
    monkeypatch.setattr(stream_mod, "session_scope", db)
    monkeypatch.setattr(stream_mod, "TERMINAL_GRACE_SECONDS", 0.05)

    async def main():
        loop_thread = threading.get_ident()
        hub = FakeHub(connected=False)
        monkeypatch.setattr(stream_mod, "POLL_SECONDS", 0.01)
        frames = await _collect(run_event_stream("run_1", 3, is_disconnected=_never_disconnected, hub=hub))
        return loop_thread, frames

    loop_thread, frames = asyncio.run(main())
    assert [f.split("\n")[0] for f in frames[:2]] == ["id: 4", "id: 5"]
    assert frames[-1].startswith("event: end") and '"COMPLETED"' in frames[-1]
    assert db.threads and loop_thread not in db.threads
    assert db.after_ids[:2] == [3, 5]  # catch-up from Last-Event-ID, then from the last id sent


def test_endpoint_authorizes_off_the_loop(monkeypatch):
    """The whole endpoint, not just the generator: the run lookup is off the loop too."""
    from analystos.api.routers import analysis

    auth_threads: list[int] = []

    @contextlib.contextmanager
    def guarded_scope():
        auth_threads.append(threading.get_ident())
        yield SimpleNamespace()

    monkeypatch.setattr(analysis, "session_scope", guarded_scope)
    monkeypatch.setattr(analysis.run_svc, "get_run_for", lambda s, user, run_id: SimpleNamespace(workspace_id="ws_1"))
    db = GuardedDb(statuses=["FAILED"], events=[[7]])
    monkeypatch.setattr(stream_mod, "session_scope", db)
    monkeypatch.setattr(stream_mod, "TERMINAL_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(stream_mod, "_hub", FakeHub())

    async def main():
        request = SimpleNamespace(headers={"last-event-id": "6"}, is_disconnected=_never_disconnected)
        response = await analysis.events("ws_1", "run_1", request, after_id=0, user=SimpleNamespace(id="usr_1"))
        return threading.get_ident(), await _collect(response.body_iterator)

    loop_thread, frames = asyncio.run(main())
    assert frames[0].startswith("id: 7") and frames[-1].startswith("event: end")
    assert auth_threads and loop_thread not in auth_threads
    assert loop_thread not in db.threads and db.after_ids[0] == 6


def test_stream_authentication_is_off_the_loop_and_short_lived(monkeypatch):
    from analystos.api import deps

    seen: list[tuple[int, bool]] = []

    @contextlib.contextmanager
    def scope():
        seen.append((threading.get_ident(), False))
        yield "session"
        seen.append((threading.get_ident(), True))  # closed before the stream starts

    monkeypatch.setattr(deps, "session_scope", scope)
    monkeypatch.setattr(deps, "_authenticate", lambda session, auth, cid: SimpleNamespace(id="usr_1", session=session))

    async def main():
        user = await deps.streaming_user("Bearer t", None)
        return threading.get_ident(), user

    loop_thread, user = asyncio.run(main())
    assert user.id == "usr_1" and [closed for _, closed in seen] == [False, True]
    assert loop_thread not in {t for t, _ in seen}


def test_endpoint_rejects_run_from_another_workspace(monkeypatch):
    from analystos.api.routers import analysis
    from analystos.core.errors import NotFound

    @contextlib.contextmanager
    def scope():
        yield SimpleNamespace()

    monkeypatch.setattr(analysis, "session_scope", scope)
    monkeypatch.setattr(analysis.run_svc, "get_run_for", lambda s, user, run_id: SimpleNamespace(workspace_id="ws_other"))
    request = SimpleNamespace(headers={}, is_disconnected=_never_disconnected)
    with pytest.raises(NotFound):
        asyncio.run(analysis.events("ws_1", "run_1", request, after_id=0, user=SimpleNamespace(id="usr_1")))


def test_nudge_wakes_the_stream_well_before_the_fallback_poll():
    store: list[dict] = []

    def fetch(run_id, after_id):
        return Batch(events=[e for e in store if e["id"] > after_id], status="RUNNING")

    async def main():
        hub = FakeHub(connected=True)  # subscribed: the fallback poll is 5 s
        agen = run_event_stream("run_1", 0, is_disconnected=_never_disconnected, hub=hub, fetch=fetch)
        first = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.2)  # the stream has caught up (nothing) and is waiting
        assert not first.done()
        store.append(_event(1))
        sent = time.monotonic()
        hub.nudge("run_1")
        frame = await asyncio.wait_for(first, 2)
        latency = time.monotonic() - sent
        await agen.aclose()
        return frame, latency, hub

    frame, latency, hub = asyncio.run(main())
    assert frame.startswith("id: 1\nevent: task.updated")
    assert latency < 0.5
    assert hub._waiters == {}  # unsubscribed on close


def test_loop_stays_responsive_while_the_database_is_slow():
    """A slow query must not stall other coroutines (the old generator blocked the loop per client)."""

    def slow_fetch(run_id, after_id):
        time.sleep(0.3)
        return Batch(events=[], status="RUNNING")

    async def main():
        hub = FakeHub(connected=True)
        streams = [run_event_stream(f"run_{i}", 0, is_disconnected=_never_disconnected, hub=hub, fetch=slow_fetch)
                   for i in range(8)]
        tasks = [asyncio.ensure_future(s.__anext__()) for s in streams]
        worst, last = 0.0, time.monotonic()
        for _ in range(30):
            await asyncio.sleep(0.01)
            now = time.monotonic()
            worst, last = max(worst, now - last - 0.01), now
        for t in tasks:
            t.cancel()
        for s in streams:
            with contextlib.suppress(BaseException):
                await s.aclose()
        return worst

    assert asyncio.run(main()) < 0.1


def test_streams_of_one_run_share_one_query():
    calls: list[tuple[str, int]] = []

    def fetch(run_id, after_id):
        calls.append((run_id, after_id))
        time.sleep(0.05)
        return Batch(events=[_event(1)], status="COMPLETED")

    async def main():
        hub = FakeHub(connected=True)
        return await asyncio.gather(*(hub.fetch("run_1", 0, fetch) for _ in range(20)), hub.fetch("run_2", 0, fetch))

    batches = asyncio.run(main())
    assert all(b.events[0]["id"] == 1 for b in batches)
    assert sorted(calls) == [("run_1", 0), ("run_2", 0)]


def test_events_route_holds_no_request_scoped_session():
    """A `db` dependency lives as long as the response: one per open stream would drain the pool."""
    from analystos.api.deps import db
    from analystos.api.routers import analysis

    route = next(r for r in analysis.router.routes if r.path.endswith("/analysis/{run_id}/events"))

    def calls(dependant):
        for dep in dependant.dependencies:
            yield dep.call
            yield from calls(dep)
    assert db not in set(calls(route.dependant))


def test_bus_nudges_only_after_commit(sqlite_db, monkeypatch):
    from analystos.db.base import session_scope
    from analystos.events import bus

    published: list[str] = []
    monkeypatch.setattr(bus, "_redis", SimpleNamespace(publish=lambda channel, msg: published.append(channel)))
    with session_scope() as s:
        bus.emit("ws_1", "task.updated", {}, run_id="run_1", session=s)
        bus.emit("ws_1", "task.updated", {}, run_id="run_1", session=s)
        s.flush()
        assert published == []  # rows are not visible to a subscriber yet
    assert published == ["run:run_1"]  # one nudge per run per commit

    with pytest.raises(RuntimeError), session_scope() as s:
        bus.emit("ws_1", "task.updated", {}, run_id="run_2", session=s)
        raise RuntimeError("rolled back")
    with session_scope() as s:
        pass
    assert published == ["run:run_1"]  # a rolled-back event is never announced

    bus.emit("ws_1", "task.updated", {}, run_id="run_3")  # own transaction: nudged after it commits
    assert published == ["run:run_1", "run:run_3"]
