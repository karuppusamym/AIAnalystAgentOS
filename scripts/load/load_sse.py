"""P4-S05 load: 500 open SSE streams (spec v3 §8 target: event latency under 1 s at p95).

    PYTHONPATH=src python scripts/load/load_sse.py [--streams 500] [--runs 50] [--seconds 30] [--suffix s05s] [--keep]
        [-- extra arguments for scripts/load_sse.py]

A thin wrapper: provisions an isolated control database (scripts/load/harness.py) and runs the
P4-C04 SSE load script (`scripts/load_sse.py`) against it at the §8 size, so both share one
measurement (private uvicorn, commit-to-client latency, loop lag, Last-Event-ID resume, `end`
frames). The evidence lands next to the other load results as sse-load-<ts>.md/.json.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--streams", type=int, default=500)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--reconnect", type=int, default=50)
    ap.add_argument("--suffix", default="s05s")
    ap.add_argument("--keep", action="store_true")
    args, extra = ap.parse_known_args()

    plane = harness.configure(args.suffix)
    harness.provision(plane)
    try:
        cmd = [sys.executable, str(harness.ROOT / "scripts" / "load_sse.py"), "--database-url", plane.control_url,
               "--streams", str(args.streams), "--runs", str(args.runs), "--seconds", str(args.seconds),
               "--reconnect", str(args.reconnect), "--out", str(harness.EVIDENCE), *[a for a in extra if a != "--"]]
        return subprocess.call(cmd, env=plane.env, cwd=harness.ROOT)
    finally:
        if not args.keep:
            from analystos.db.base import get_engine

            get_engine().dispose()
            harness.teardown(plane)


if __name__ == "__main__":
    sys.exit(main())
