"""P4-S05 load: 1,000 workspaces (spec v3 §8 target) through the HTTP API.

    PYTHONPATH=src python scripts/load/load_workspaces.py [--workspaces 1000] [--concurrency 32] [--seconds 30]
        [--api-workers 2] [--suffix s05w] [--keep]

On an isolated plane (scripts/load/harness.py) with a private uvicorn: creates `--workspaces`
workspaces through `POST /api/workspaces` (each with its policy and owner membership), makes the
analyst a member of 10 of them, then drives a read mix from `--concurrency` clients for `--seconds`
(workspace list and detail, runs, approvals, artifacts, semantic model, builds, activity) against
random workspaces. Measured: create latency and rate, per-endpoint p50/p95, requests per second,
errors, machine load and API RSS/CPU, Postgres connections. Tenancy is checked at this size: the
analyst lists exactly their 10 workspaces and is refused every other one sampled.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

PASSWORD = "ChangeMe123!"
READS = [
    ("workspace", "/api/workspaces/{ws}"),
    ("runs", "/api/workspaces/{ws}/analysis"),
    ("approvals", "/api/workspaces/{ws}/approvals"),
    ("artifacts", "/api/workspaces/{ws}/artifacts"),
    ("semantic", "/api/workspaces/{ws}/semantic"),
    ("builds", "/api/workspaces/{ws}/builds"),
    ("activity", "/api/workspaces/{ws}/activity"),
]


async def login(client, base: str, email: str) -> dict:
    r = await client.post(f"{base}/api/auth/login", json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def create_all(client, base: str, headers: dict, n: int, concurrency: int) -> tuple[list[str], list[float], list[str], float]:
    sem = asyncio.Semaphore(concurrency)
    ids: list[str] = []
    lat: list[float] = []
    errors: list[str] = []

    async def one(i: int):
        async with sem:
            t = time.perf_counter()
            try:
                r = await client.post(f"{base}/api/workspaces", headers=headers,
                                      json={"name": f"tenant {i:04d}", "objective": "Load test tenant: incident analytics"})
                lat.append(time.perf_counter() - t)
                if r.status_code == 200:
                    ids.append(r.json()["id"])
                else:
                    errors.append(f"HTTP {r.status_code}")
            except Exception as exc:
                errors.append(type(exc).__name__)
    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(n)))
    return ids, lat, errors, time.perf_counter() - t0


async def read_mix(client, base: str, headers: dict, ids: list[str], concurrency: int, seconds: float):
    lat: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    stop = time.time() + seconds
    rng = random.Random(7)

    async def worker():
        while time.time() < stop:
            name, path = rng.choice(READS)
            url = base + path.format(ws=rng.choice(ids))
            t = time.perf_counter()
            try:
                r = await client.get(url, headers=headers)
                lat[name].append(time.perf_counter() - t)
                if r.status_code != 200:
                    errors[f"{name} HTTP {r.status_code}"] += 1
            except Exception as exc:
                errors[f"{name} {type(exc).__name__}"] += 1
            if rng.random() < 0.05:  # the landing list, which grows with the tenant count
                t = time.perf_counter()
                try:
                    r = await client.get(f"{base}/api/workspaces", headers=headers)
                    lat["workspace_list"].append(time.perf_counter() - t)
                    if r.status_code != 200:
                        errors[f"workspace_list HTTP {r.status_code}"] += 1
                except Exception as exc:
                    errors[f"workspace_list {type(exc).__name__}"] += 1
    t0 = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return lat, errors, time.perf_counter() - t0


async def tenancy(client, base: str, admin: dict, analyst: dict, ids: list[str]) -> dict:
    member_of = ids[:10]
    for ws in member_of:
        r = await client.post(f"{base}/api/workspaces/{ws}/members", headers=admin, json={"email": "analyst@analystos.local", "role": "analyst"})
        r.raise_for_status()
    listed = {w["id"] for w in (await client.get(f"{base}/api/workspaces", headers=analyst)).json()}
    others = random.Random(3).sample(ids[10:], min(50, len(ids) - 10))
    refused = 0
    for ws in others:
        codes = {(await client.get(base + p.format(ws=ws), headers=analyst)).status_code for _, p in READS[:3]}
        refused += all(c in (403, 404) for c in codes)
    return {"analyst_lists_exactly_their_workspaces": listed == set(member_of), "listed": len(listed),
            "foreign_workspaces_sampled": len(others), "foreign_workspaces_refused": refused}


async def run(base: str, args) -> dict:
    import httpx

    limits = httpx.Limits(max_connections=args.concurrency + 8, max_keepalive_connections=args.concurrency + 8)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), limits=limits) as client:
        admin = await login(client, base, "admin@analystos.local")
        analyst = await login(client, base, "analyst@analystos.local")
        ids, create_lat, create_err, create_wall = await create_all(client, base, admin, args.workspaces, min(16, args.concurrency))
        t = time.perf_counter()
        listed = (await client.get(f"{base}/api/workspaces", headers=admin)).json()
        list_once = time.perf_counter() - t
        lat, errors, wall = await read_mix(client, base, admin, ids, args.concurrency, args.seconds)
        ten = await tenancy(client, base, admin, analyst, ids)
    total = sum(len(v) for v in lat.values())
    return {
        "workspaces_created": len(ids), "create_errors": sorted(set(create_err))[:5], "create_latency_ms": harness.summary(create_lat),
        "create_wall_s": round(create_wall, 1), "creates_per_s": round(len(ids) / create_wall, 1) if create_wall else None,
        "admin_list_rows": len(listed), "admin_list_once_ms": round(list_once * 1000, 1),
        "read_mix": {name: harness.summary(v) for name, v in sorted(lat.items())},
        "read_requests": total, "read_errors": dict(errors), "read_rps": round(total / wall, 1) if wall else None,
        "read_latency_all_ms": harness.summary([x for v in lat.values() for x in v]), "tenancy": ten,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspaces", type=int, default=1000)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--api-workers", type=int, default=2)
    ap.add_argument("--suffix", default="s05w")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--label", default="", help="appended to the evidence file name (e.g. before-fix)")
    args = ap.parse_args()

    plane = harness.configure(args.suffix)
    started_at = datetime.now(UTC)
    harness.provision(plane)
    api, base = harness.start_api(plane, workers=args.api_workers)
    result: dict = {"started_at": started_at.isoformat(), "label": args.label, "target": "1,000 workspaces", "workspaces": args.workspaces,
                    "concurrency": args.concurrency, "seconds": args.seconds, "api_workers": args.api_workers}
    try:
        with harness.Sampler([api.pid], every=1.0, database=plane.control_db) as sampler:
            result.update(asyncio.run(run(base, args)))
        result["resources"] = sampler.report()
        from sqlalchemy import text

        from analystos.db.base import get_engine

        with get_engine().connect() as c:
            result["control_plane_rows"] = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                                            for t in ("workspace", "workspace_member", "workspace_policy", "audit_event", "run_event")}
        rm = result["read_mix"]
        result["checks"] = {
            f"{args.workspaces}_workspaces_created": result["workspaces_created"] == args.workspaces and not result["create_errors"],
            "no_read_errors": not result["read_errors"],
            "tenancy_holds": result["tenancy"]["analyst_lists_exactly_their_workspaces"]
            and result["tenancy"]["foreign_workspaces_refused"] == result["tenancy"]["foreign_workspaces_sampled"],
            "workspace_reads_p95_under_1s": all((v["p95"] or 0) < 1000 for k, v in rm.items() if k != "workspace_list"),
            "workspace_list_p95_under_1s": (rm.get("workspace_list", {}).get("p95") or 10**9) < 1000,
        }
    finally:
        harness.stop([api])
        if not args.keep:
            from analystos.db.base import get_engine

            get_engine().dispose()
            harness.teardown(plane)
    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    path = harness.write_json(f"load-workspaces-{stamp}{'-' + args.label if args.label else ''}.json", result)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
