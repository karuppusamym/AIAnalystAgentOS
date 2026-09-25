"""Server-Sent Events for run event streams (§42), without blocking the event loop.

Rows in `run_event` stay the source of truth. Each stream catches up from the database by id
(`after_id` / `Last-Event-ID`), then sleeps until a Redis nudge for its run arrives
(`events/bus.py` publishes one after the emitting transaction commits) or a fallback poll interval
passes. Every database read runs in a worker thread under a small capacity limiter, so a slow
database or many clients cannot stall the loop or exhaust the connection pool.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
from sqlalchemy import select

from analystos.contracts.events import RUN_TERMINAL
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, RunEvent

log = get_logger(__name__)
BATCH = 200
POLL_SECONDS = 0.5  # no Redis subscription: poll (off the loop) as often as before
FALLBACK_POLL_SECONDS = 5.0  # subscribed: poll only as a safety net for a lost nudge
KEEPALIVE_SECONDS = 15.0
TERMINAL_GRACE_SECONDS = 1.0  # events emitted just after the terminal status still reach the client
_db_limiter: anyio.CapacityLimiter | None = None


def db_limiter() -> anyio.CapacityLimiter:
    global _db_limiter
    if _db_limiter is None:
        _db_limiter = anyio.CapacityLimiter(8)
    return _db_limiter


async def run_blocking(fn: Callable[..., Any], *args: Any) -> Any:
    """Run synchronous (database) code in a worker thread, never on the event loop."""
    return await anyio.to_thread.run_sync(fn, *args, limiter=db_limiter())


@dataclass
class Batch:
    events: list[dict[str, Any]]
    status: str | None


def fetch_batch(run_id: str, after_id: int, limit: int = BATCH) -> Batch:
    """Blocking: status first, then events, so a terminal status implies its events are visible."""
    with session_scope() as s:
        status = s.scalar(select(AnalysisRun.status).where(AnalysisRun.id == run_id))
        rows = s.execute(select(RunEvent.id, RunEvent.type, RunEvent.payload, RunEvent.actor, RunEvent.created_at)
                         .where(RunEvent.run_id == run_id, RunEvent.id > after_id).order_by(RunEvent.id).limit(limit))
        events = [{"id": i, "type": t, "payload": p, "actor": a, "created_at": c.isoformat() if c else None}
                  for i, t, p, a, c in rows]
    return Batch(events=events, status=status)


class RunEventHub:
    """One Redis pattern subscription per process; nudges wake the streams waiting on that run."""

    def __init__(self, redis_url: str | None = None):
        self._redis_url = redis_url
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: dict[tuple[str, int], asyncio.Future] = {}
        self.connected = False

    async def fetch(self, run_id: str, after_id: int, fetch: Callable[[str, int], Batch]) -> Batch:
        """Streams of one run wake together on its nudge and usually share a cursor: one query serves them all."""
        key = (run_id, after_id)
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            batch = await run_blocking(fetch, run_id, after_id)
            future.set_result(batch)
            return batch
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # retrieved: waiters re-raise it, an unawaited future does not warn
            raise
        finally:
            self._inflight.pop(key, None)

    def subscribe(self, run_id: str) -> asyncio.Event:
        self._ensure_listener()
        wake = asyncio.Event()
        self._waiters.setdefault(run_id, set()).add(wake)
        return wake

    def unsubscribe(self, run_id: str, wake: asyncio.Event) -> None:
        waiters = self._waiters.get(run_id)
        if waiters is not None:
            waiters.discard(wake)
            if not waiters:
                self._waiters.pop(run_id, None)

    def nudge(self, run_id: str) -> None:
        for wake in self._waiters.get(run_id, ()):
            wake.set()

    @property
    def poll_seconds(self) -> float:
        return FALLBACK_POLL_SECONDS if self.connected else POLL_SECONDS

    def _ensure_listener(self) -> None:
        loop = asyncio.get_running_loop()
        if self._task is not None and not self._task.done() and self._loop is loop:
            return
        self._loop, self.connected = loop, False
        self._task = loop.create_task(self._listen())

    async def _listen(self) -> None:
        try:
            import redis.asyncio as aioredis
        except Exception:  # pragma: no cover - redis is a core dependency
            return
        url = self._redis_url
        if url is None:
            from analystos.core.config import get_settings

            url = get_settings().redis_url
        backoff = 1.0
        while True:
            client = aioredis.Redis.from_url(url, socket_connect_timeout=1)
            try:
                async with client.pubsub() as pubsub:
                    await pubsub.psubscribe("run:*")
                    self.connected, backoff = True, 1.0
                    for waiters in list(self._waiters.values()):  # anything missed while disconnected
                        for wake in waiters:
                            wake.set()
                    async for message in pubsub.listen():
                        if message.get("type") != "pmessage":
                            continue
                        channel = message.get("channel")
                        channel = channel.decode() if isinstance(channel, bytes) else str(channel)
                        self.nudge(channel.split(":", 1)[1])
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:
                if self.connected:
                    log.warning("run event subscription lost: %s", exc)
                self.connected = False
            finally:
                with contextlib.suppress(Exception):
                    await client.aclose()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


_hub: RunEventHub | None = None


def default_hub() -> RunEventHub:
    global _hub
    if _hub is None:
        _hub = RunEventHub()
    return _hub


def _sse(event: dict[str, Any]) -> str:
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


async def run_event_stream(run_id: str, after_id: int, *, is_disconnected: Callable[[], Awaitable[bool]],
                           hub: RunEventHub | None = None,
                           fetch: Callable[[str, int], Batch] = fetch_batch) -> AsyncIterator[str]:
    """SSE frames for one run: DB catch-up by id, then wake on nudge or fallback poll."""
    hub = hub or default_hub()
    wake = hub.subscribe(run_id)
    last = after_id
    last_sent = time.monotonic()
    terminal_since: float | None = None
    try:
        while not await is_disconnected():
            wake.clear()  # a nudge that lands during the fetch below re-arms the wait
            batch = await hub.fetch(run_id, last, fetch)
            for event in batch.events:
                last = event["id"]
                yield _sse(event)
            now = time.monotonic()
            if batch.events:
                last_sent = now
                if len(batch.events) >= BATCH:
                    continue
            if batch.status in RUN_TERMINAL or batch.status is None:
                terminal_since = terminal_since or now
                if not batch.events and (batch.status is None or now - terminal_since >= TERMINAL_GRACE_SECONDS):
                    yield f"event: end\ndata: {json.dumps({'status': batch.status})}\n\n"
                    return
            if now - last_sent >= KEEPALIVE_SECONDS:
                last_sent = now
                yield ": keep-alive\n\n"
            timeout = TERMINAL_GRACE_SECONDS if terminal_since else hub.poll_seconds
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout)
    finally:
        hub.unsubscribe(run_id, wake)
