"""P4-E05 spike: dlt versus the staged loader for incremental ingestion from a non-SQL source.

Measures the same ServiceNow Table API (the mock, `uvicorn analystos.connectors.servicenow_mock:app`)
loaded into Postgres two ways, and prints one JSON object per measurement:

  --mode staged  the current path: ServiceNowConnector.extract -> StagingLoader.load (COPY into a
                 load table, atomic swap). Every refresh is a full reload. Run with the platform venv
                 and PYTHONPATH=src.
  --mode dlt     a dlt resource over the same API with `dlt.sources.incremental("sys_updated_on")`
                 and merge on sys_id. Run with a separate venv holding `dlt[postgres]` (it does not
                 import analystos).

Both write only into the scratch database given by --db-url. Used for docs/10-architecture/adr/0016.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from typing import Any

TABLE = "incident"


def _counting_client(counter: dict[str, int]):  # noqa: ANN202
    import httpx

    def hook(response: httpx.Response) -> None:
        response.read()
        counter["requests"] += 1
        counter["bytes"] += len(response.content)

    return httpx.Client(timeout=60, event_hooks={"response": [hook]})


def staged(args: argparse.Namespace) -> list[dict[str, Any]]:
    from types import SimpleNamespace

    from analystos.connectors.servicenow import ServiceNowConnector
    from analystos.staging.loader import StagingLoader

    settings = SimpleNamespace(analytics_loader_url=args.db_url, analytics_workspace_role_prefix="spike_e05_r_")
    loader = StagingLoader(settings, loader_url=args.db_url, reader_role=args.reader_role)
    out = []
    for label in ("initial full load", "refresh (no source change): full reload", "refresh (5% changed): full reload"):
        counter = {"requests": 0, "bytes": 0}
        conn = ServiceNowConnector({"instance_url": args.url, "username": "admin", "tables": [TABLE], "page_size": 1000,
                                    "display_values": not args.raw}, password="admin", http_client=_counting_client(counter))
        asset = next(a for a in conn.discover() if a.name == TABLE)
        discover_requests, discover_bytes = counter["requests"], counter["bytes"]
        started = time.perf_counter()
        info = loader.load("spike", asset, conn.extract(asset, max_rows=1_000_000), workspace_id="e05")
        out.append({"mode": "staged" + (" (raw values)" if args.raw else " (display values)"), "run": label, "seconds": round(time.perf_counter() - started, 2),
                    "rows_transferred": info["row_count"], "rows_in_table": info["row_count"],
                    "http_requests": counter["requests"] - discover_requests, "http_bytes": counter["bytes"] - discover_bytes,
                    "columns": len(info["columns"])})
    return out


def dlt_mode(args: argparse.Namespace) -> list[dict[str, Any]]:
    import dlt
    import psycopg2
    import requests

    counter = {"requests": 0, "bytes": 0}
    session = requests.Session()
    session.auth = ("admin", "admin")

    def page(query: str, offset: int) -> list[dict[str, Any]]:
        r = session.get(f"{args.url}/api/now/table/{TABLE}", timeout=60,
                        params={"sysparm_limit": 1000, "sysparm_offset": offset, "sysparm_query": query,
                                "sysparm_exclude_reference_link": "true", "sysparm_display_value": "false"})
        r.raise_for_status()
        counter["requests"] += 1
        counter["bytes"] += len(r.content)
        return r.json()["result"]

    def make(initial: str):  # noqa: ANN202
        @dlt.resource(name=TABLE, primary_key="sys_id", write_disposition="merge")
        def incident(updated=dlt.sources.incremental("sys_updated_on", initial_value=initial)):  # noqa: B008
            offset, since = 0, updated.last_value  # fixed for the whole pagination (last_value moves as rows are yielded)
            while True:
                rows = page(f"sys_updated_on>={since}^ORDERBYsys_updated_on", offset)
                if rows:
                    yield rows
                if len(rows) < 1000:
                    break
                offset += 1000
        return incident

    def count(dataset: str) -> int:
        with psycopg2.connect(args.db_url.replace("postgresql+psycopg://", "postgresql://")) as c, c.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {dataset}.{TABLE}")
            return cur.fetchone()[0]

    def run(label: str, pipeline_name: str, dataset: str, initial: str) -> dict[str, Any]:
        before = dict(counter)
        pipe = dlt.pipeline(pipeline_name=pipeline_name, pipelines_dir=pipes, dataset_name=dataset,
                            destination=dlt.destinations.postgres(args.db_url.replace("postgresql+psycopg://", "postgresql://")))
        started = time.perf_counter()
        info = pipe.run(make(initial)())
        seconds = round(time.perf_counter() - started, 2)
        trace = pipe.last_trace
        rows = sum(v for k, v in (trace.last_normalize_info.row_counts if trace and trace.last_normalize_info else {}).items()
                   if k == TABLE)
        return {"mode": "dlt", "run": label, "seconds": seconds, "rows_transferred": rows, "rows_in_table": count(dataset),
                "http_requests": counter["requests"] - before["requests"], "http_bytes": counter["bytes"] - before["bytes"],
                "load_packages": len(info.load_packages), "dlt": dlt.__version__}

    pipes = tempfile.mkdtemp(prefix="spike_e05_dlt_")
    out = [run("initial full load", "spike_sn", "dlt_sn", "1970-01-01 00:00:00"),
           run("refresh (no source change): incremental", "spike_sn", "dlt_sn", "1970-01-01 00:00:00")]
    # A 5% change window: the newest 5% of rows by sys_updated_on, i.e. what an incremental run fetches
    # and merges after that many records changed (the mock's data is static, so the window is replayed).
    first = page("ORDERBYDESCsys_updated_on", int(out[0]["rows_in_table"] * 0.05))
    cutoff = first[0]["sys_updated_on"]
    out.append(run("refresh (5% changed): incremental window, cold state", "spike_sn_5pct", "dlt_sn_5pct", cutoff))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["staged", "dlt"], required=True)
    p.add_argument("--url", default="http://127.0.0.1:8095")
    p.add_argument("--db-url", required=True)
    p.add_argument("--reader-role", default="spike_e05_reader")
    p.add_argument("--raw", action="store_true", help="staged: raw values only (no display values), like the dlt run")
    args = p.parse_args()
    for row in (staged(args) if args.mode == "staged" else dlt_mode(args)):
        print(json.dumps(row))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
