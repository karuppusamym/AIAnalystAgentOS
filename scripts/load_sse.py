"""SSE load test (P4-C04): many concurrent run-event streams against a private API process.

    python scripts/load_sse.py --database-url postgresql+psycopg://analystos:analystos@localhost:5432/analystos_test_c \
        [--streams 200] [--runs 50] [--seconds 30] [--rate 1.0] [--reconnect 20] [--no-redis] [--out docs/60-delivery/evidence]

Starts its own uvicorn on a free port against the given (test) database, never the running API.
It inserts a user, a workspace and `--runs` RUNNING runs, opens `--streams` SSE clients spread over
them, and emits one `load.tick` event per run every 1/rate seconds from a separate writer (as a
worker would). Latency is receipt time minus the emitter's timestamp taken *before* its commit.
The server counts asyncio slow-callback ("stall") warnings -- a callback holding the loop for
>= 100 ms -- samples its loop lag every 50 ms, and times full GC collections to attribute lag. `--reconnect` streams drop halfway and
resume with Last-Event-ID; every stream must receive every tick of its run exactly once. At the
end the runs complete and every stream must get its `end` frame. Writes sse-load-<ts>.md/.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

LAG_INTERVAL = 0.05


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q / 100 * (len(ordered) - 1))))]


# ---------------------------------------------------------------------------------------- server
def serve(port: int) -> None:
    """The API as deployed, plus a loop-lag probe the load client reads at the end."""
    import gc

    import uvicorn

    from analystos.api.app import app

    samples: list[float] = []
    stalls: list[tuple[float, float]] = []
    gc_pauses: list[tuple[float, int, float]] = []
    gc_started: dict[str, float] = {}

    def gc_timer(phase, info):  # attribute stalls: a full collection also pauses the loop
        if phase == "start":
            gc_started["t"] = time.perf_counter()
        elif "t" in gc_started:
            ms = (time.perf_counter() - gc_started.pop("t")) * 1000
            if ms > 20:
                gc_pauses.append((time.time(), info["generation"], round(ms, 1)))
    gc.callbacks.append(gc_timer)

    async def probe():
        while True:
            started = time.perf_counter()
            await asyncio.sleep(LAG_INTERVAL)
            lag = max(0.0, time.perf_counter() - started - LAG_INTERVAL)
            samples.append(lag)
            if lag > 0.1:
                stalls.append((time.time(), round(lag * 1000, 1)))

    # asyncio's own "stall warning" (debug mode's slow-callback check) without debug overhead: any
    # single callback or task step that holds the loop for >= 100 ms. Needs the stdlib loop, so the
    # measured server runs on asyncio rather than uvloop (the slower of the two).
    slow: list[tuple[float, float, str]] = []
    handle_run = asyncio.events.Handle._run

    def timed_run(handle):
        started = time.perf_counter()
        handle_run(handle)
        took = time.perf_counter() - started
        if took >= 0.1:
            slow.append((time.time(), round(took * 1000, 1), repr(handle)[:160]))
    asyncio.events.Handle._run = timed_run

    from contextlib import asynccontextmanager

    app_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_probe(a):
        async with app_lifespan(a):
            task = asyncio.get_running_loop().create_task(probe())
            yield
            task.cancel()

    def lag():
        values = list(samples)
        return {"samples": len(values), "p50_ms": round((_pct(values, 50) or 0) * 1000, 2),
                "p95_ms": round((_pct(values, 95) or 0) * 1000, 2), "p99_ms": round((_pct(values, 99) or 0) * 1000, 2),
                "max_ms": round(max(values, default=0) * 1000, 2), "stalls_over_100ms": sum(1 for v in values if v > 0.1),
                "stalls": stalls, "slow_callbacks": slow, "gc_pauses_over_20ms": gc_pauses, "gc_frozen": gc.get_freeze_count(),
                "gc_tracked": len(gc.get_objects())}

    def reset():  # the measurement window is the load, not the server's warm-up
        for series in (samples, stalls, slow, gc_pauses):
            series.clear()
        return {"ok": True}

    app.router.lifespan_context = lifespan_with_probe
    app.add_api_route("/__load/lag", lag, methods=["GET"])
    app.add_api_route("/__load/reset", reset, methods=["POST"])
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")


# ---------------------------------------------------------------------------------------- setup
def setup(runs: int) -> tuple[str, str, list[str]]:
    from sqlalchemy import select, text

    from analystos.core.ids import new_id
    from analystos.db import models
    from analystos.db.base import get_engine, session_scope
    from analystos.security.auth import hash_password

    engine = get_engine()
    with engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    models.Base.metadata.create_all(engine)
    with session_scope() as s:
        user = s.scalar(select(models.User).where(models.User.email == "sse-load@analystos.local"))
        if user is None:
            user = models.User(id=new_id("usr"), email="sse-load@analystos.local", name="SSE load", attributes={},
                               password_hash=hash_password(new_id("pw")))
            s.add(user)
            s.flush()
        ws = models.Workspace(id=new_id("ws"), name="sse load", objective="load test", created_by=user.id, settings={})
        s.add(ws)
        s.flush()
        s.add(models.WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        run_ids = []
        for _ in range(runs):
            run = models.AnalysisRun(id=new_id("run"), workspace_id=ws.id, objective="load test", status="RUNNING",
                                     plan={"steps": []}, plan_version=1, scope={}, instructions=[], constraints={},
                                     requested_by=user.id, summary={}, origin={"type": "load"})
            s.add(run)
            run_ids.append(run.id)
        return user.id, ws.id, run_ids


def emitter(ws: str, run_ids: list[str], seconds: float, rate: float, sent: dict[str, int], stop: threading.Event) -> None:
    """A worker-like writer: one committed event per run per tick, each in its own transaction."""
    from analystos.db.base import session_scope
    from analystos.events.bus import emit

    deadline = time.time() + seconds
    while time.time() < deadline and not stop.is_set():
        tick = time.time()
        for run_id in run_ids:
            with session_scope() as s:
                emit(ws, "load.tick", {"seq": sent[run_id], "t": time.time()}, run_id=run_id, session=s)
            sent[run_id] += 1
        time.sleep(max(0.0, 1 / rate - (time.time() - tick)))


def finish_runs(ws: str, run_ids: list[str]) -> None:
    from sqlalchemy import update

    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun
    from analystos.events.bus import emit

    for run_id in run_ids:
        with session_scope() as s:
            s.execute(update(AnalysisRun).where(AnalysisRun.id == run_id).values(status="COMPLETED"))
            emit(ws, "run.status", {"status": "COMPLETED"}, run_id=run_id, session=s)


# ---------------------------------------------------------------------------------------- client
class Stream:
    def __init__(self, idx: int, run_id: str):
        self.idx, self.run_id = idx, run_id
        self.latencies: list[float] = []
        self.seqs: list[int] = []
        self.last_id: str | None = None
        self.ended = False
        self.connects = 0
        self.connect_s: list[float] = []
        self.error: str | None = None


async def consume(client, base: str, ws: str, headers: dict, stream: Stream, stop_at: float | None) -> None:
    """Read one SSE connection; return early (to reconnect) once `stop_at` passes."""
    url = f"{base}/api/workspaces/{ws}/analysis/{stream.run_id}/events"
    h = dict(headers)
    if stream.last_id:
        h["Last-Event-ID"] = stream.last_id
    stream.connects += 1
    started = time.perf_counter()
    async with client.stream("GET", url, headers=h) as r:
        stream.connect_s.append(time.perf_counter() - started)
        if r.status_code != 200:
            stream.error = f"HTTP {r.status_code}"
            return
        event, data, eid = None, None, None
        async for line in r.aiter_lines():
            if line.startswith("id: "):
                eid = line[4:]
            elif line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
            elif line == "":
                if event == "end":
                    stream.ended = True
                    return
                if event == "load.tick" and data:
                    body = json.loads(data)
                    stream.latencies.append(time.time() - body["payload"]["t"])
                    stream.seqs.append(body["payload"]["seq"])
                if eid:
                    stream.last_id = eid
                event, data, eid = None, None, None
                if stop_at is not None and time.time() >= stop_at:
                    return


async def client_main(base: str, ws: str, token: str, streams: list[Stream], reconnect_at: float, reconnecting: set[int]) -> None:
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    limits = httpx.Limits(max_connections=len(streams) + 20, max_keepalive_connections=len(streams) + 20)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None), limits=limits) as client:
        async def one(stream: Stream):
            try:
                await consume(client, base, ws, headers, stream, reconnect_at if stream.idx in reconnecting else None)
                if not stream.ended and stream.error is None:
                    await consume(client, base, ws, headers, stream, None)
            except Exception as exc:  # recorded, not raised: one failing stream must not hide the others
                stream.error = f"{type(exc).__name__}: {exc}"
        await asyncio.gather(*(one(s) for s in streams))


# ---------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--database-url", default=os.getenv("ANALYSTOS_LOAD_DATABASE_URL"))
    ap.add_argument("--streams", type=int, default=200)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--rate", type=float, default=1.0, help="events per run per second")
    ap.add_argument("--reconnect", type=int, default=20, help="streams that drop halfway and resume with Last-Event-ID")
    ap.add_argument("--no-redis", action="store_true", help="measure the polling fallback (Redis unreachable)")
    ap.add_argument("--out", default="docs/60-delivery/evidence")
    args = ap.parse_args()
    if args.serve:
        serve(args.serve)
        return 0
    if not args.database_url:
        raise SystemExit("--database-url (a test database) is required")
    if args.database_url.rsplit("/", 1)[-1].split("?")[0] == "analystos":
        raise SystemExit("refusing to load-test the main 'analystos' database; use a test database")
    os.environ["ANALYSTOS_DATABASE_URL"] = args.database_url
    os.environ.setdefault("ANALYSTOS_JWT_SECRET", "sse-load-secret-sse-load-secret-123456")
    if args.no_redis:
        os.environ["ANALYSTOS_REDIS_URL"] = f"redis://127.0.0.1:{_free_port()}/0"
    from analystos.security.auth import issue_token

    user_id, ws, run_ids = setup(args.runs)
    token = issue_token(user_id, "sse-load@analystos.local")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([sys.executable, __file__, "--serve", str(port)], env=dict(os.environ))
    try:
        import httpx

        for _ in range(200):
            try:
                if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise SystemExit("API did not start")
        streams = [Stream(i, run_ids[i % len(run_ids)]) for i in range(args.streams)]
        reconnecting = set(range(0, args.streams, max(1, args.streams // max(1, args.reconnect))))
        reconnecting = set(sorted(reconnecting)[: args.reconnect])
        started = datetime.now(UTC)
        sent = {r: 0 for r in run_ids}
        stop = threading.Event()
        warmup = 3.0  # let every stream connect and catch up before the first tick

        def writer():
            time.sleep(warmup)
            emitter(ws, run_ids, args.seconds, args.rate, sent, stop)
            time.sleep(1.0)
            finish_runs(ws, run_ids)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        httpx.post(f"{base}/__load/reset", timeout=5).raise_for_status()
        t0, client_start = time.perf_counter(), time.time()
        asyncio.run(asyncio.wait_for(client_main(base, ws, token, streams, time.time() + warmup + args.seconds / 2, reconnecting),
                                     timeout=warmup + args.seconds + 60))
        wall = time.perf_counter() - t0
        stop.set()
        thread.join(timeout=10)
        lag = httpx.get(f"{base}/__load/lag", timeout=5).json()
    finally:
        server.terminate()
        server.wait(timeout=10)

    latencies = [v for s in streams for v in s.latencies]
    expected = sum(sent[s.run_id] for s in streams)
    complete = [s for s in streams if s.seqs == list(range(sent[s.run_id]))]
    recon = [s for s in streams if s.idx in reconnecting]
    connects = [v for s in streams for v in s.connect_s]
    result = {
        "started_at": started.isoformat(), "database": args.database_url.rsplit("@", 1)[-1], "redis": not args.no_redis,
        "streams": args.streams, "runs": args.runs, "seconds": args.seconds, "rate_per_run": args.rate,
        "events_emitted": sum(sent.values()), "deliveries_expected": expected, "deliveries_received": len(latencies),
        "streams_complete_in_order": len(complete), "streams_ended_cleanly": sum(1 for s in streams if s.ended),
        "reconnected_streams": len(recon), "reconnected_complete": sum(1 for s in recon if s in complete and s.connects == 2),
        "errors": sorted({s.error for s in streams if s.error}),
        "latency_ms": {k: round((_pct(latencies, q) or 0) * 1000, 1) for k, q in (("p50", 50), ("p95", 95), ("p99", 99))}
        | {"max": round(max(latencies, default=0) * 1000, 1)},
        "connect_ms_p95": round((_pct(connects, 95) or 0) * 1000, 1),
        "loop_lag": {k: v for k, v in lag.items() if k not in ("stalls", "gc_pauses_over_20ms", "slow_callbacks")},
        "slow_callback_warnings": [[round(t - client_start, 1), ms, what] for t, ms, what in lag["slow_callbacks"]],
        "client_wall_seconds": round(wall, 1),
        "gc_pauses_at_s": [[round(t - client_start, 1), gen, ms] for t, gen, ms in lag["gc_pauses_over_20ms"]],
        # seconds after the clients started: 0-3 connect + catch-up, then ticks; reconnects at 3 + seconds/2
        "stalls_at_s": [[round(t - client_start, 1), ms] for t, ms in lag["stalls"]],
    }
    passed = {
        "p95_latency_below_1s": result["latency_ms"]["p95"] < 1000,
        "no_event_loop_stall_warnings": not lag["slow_callbacks"],
        "every_delivery_received_once_in_order": len(complete) == args.streams and len(latencies) == expected,
        "resume_with_last_event_id": result["reconnected_complete"] == len(recon),
        "every_stream_ended": result["streams_ended_cleanly"] == args.streams,
        "no_errors": not result["errors"],
    }
    result["checks"] = passed
    print(json.dumps(result, indent=2))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    (out / f"sse-load-{stamp}.json").write_text(json.dumps(result, indent=2))
    lines = [f"# SSE load evidence — {started:%Y-%m-%d %H:%M} UTC", "",
             f"`python scripts/load_sse.py --streams {args.streams} --runs {args.runs} --seconds {args.seconds:g} --rate {args.rate:g} "
             f"--reconnect {args.reconnect}{' --no-redis' if args.no_redis else ''}` against a private uvicorn on a test "
             f"database (`{result['database']}`), not the running API. Redis nudges: {'on' if result['redis'] else 'off (polling fallback)'}. "
             f"Raw: `sse-load-{stamp}.json`. Server measurements cover the load window (first client connect to the "
             f"last `end` frame), not the server's warm-up; the measured server runs the stdlib asyncio loop so "
             f"slow callbacks can be timed (production uvicorn uses uvloop, which is faster).", "",
             f"**{sum(passed.values())}/{len(passed)} checks passed.**", "", "| Check | Result |", "|---|---|"]
    lines += [f"| {k} | {'PASS' if v else '**FAIL**'} |" for k, v in passed.items()]
    lat, lg = result["latency_ms"], result["loop_lag"]
    lines += ["", "| Measure | Value |", "|---|---|",
              f"| Concurrent streams / runs | {args.streams} / {args.runs} |",
              f"| Events emitted / deliveries expected / received | {result['events_emitted']} / {expected} / {len(latencies)} |",
              f"| Event latency p50 / p95 / p99 / max (commit→client, ms) | {lat['p50']} / {lat['p95']} / {lat['p99']} / {lat['max']} |",
              f"| Server loop lag p50 / p95 / p99 / max (ms, {lg['samples']} samples @ 50 ms) | "
              f"{lg['p50_ms']} / {lg['p95_ms']} / {lg['p99_ms']} / {lg['max_ms']} |",
              f"| Slow-callback (stall) warnings: one callback holding the loop >= 100 ms | "
              f"{len(result['slow_callback_warnings'])} |",
              f"| Probe lag samples > 100 ms (s after connect: ms) | {lg['stalls_over_100ms']} "
              f"{', '.join(f'({t} s: {ms} ms)' for t, ms in result['stalls_at_s'])} |",
              f"| Full (gen-2) GC pauses > 20 ms (s: ms) | "
              f"{', '.join(f'({t} s: {ms} ms)' for t, gen, ms in result['gc_pauses_at_s'] if gen == 2) or 'none'} |",
              f"| Streams complete and in order | {len(complete)}/{args.streams} |",
              f"| Streams resumed with Last-Event-ID, complete | {result['reconnected_complete']}/{len(recon)} |",
              f"| Streams ended with `end` frame | {result['streams_ended_cleanly']}/{args.streams} |",
              f"| Stream connect (auth + response headers) p95, ms | {result['connect_ms_p95']} |"]
    if result["errors"]:
        lines += ["", "Errors: " + "; ".join(result["errors"])]
    (out / f"sse-load-{stamp}.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {out / f'sse-load-{stamp}.md'}")
    return 0 if all(passed.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
