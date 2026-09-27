"""Token economy of the standard flow (Stream B): what every model call sends, how much of it is the
stable cache-flagged prefix, and how much context work the shared context cache saved — before vs after.

Runs `tests/integration/test_context_economy.py` (fake transport: no network, no real model; needs the
compose Postgres) and writes its JSON report, or renders the comparison of two reports:

    # measure this tree (isolated test databases)
    ANALYSTOS_TEST_DATABASE_URL=postgresql+psycopg://analystos:analystos@localhost:5432/analystos_test_sb \\
    ANALYSTOS_TEST_DP_DB=analystos_test_dp_sb \\
      python scripts/measure_context_economy.py --out after.json

    # compare (e.g. a report taken on the base commit with the same harness)
    python scripts/measure_context_economy.py --before before.json --after after.json [--md table.md]

Tokens are estimates (characters / 3.6, `llm.cache.estimate_tokens`). The stable-prefix share is the
cache-flagged prefix over all characters sent; the reusable share is the longest prefix each request
shares with an earlier request of the flow (an upper bound on automatic prefix caching)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def measure(out: Path) -> dict:
    env = {**os.environ, "ANALYSTOS_ECONOMY_OUT": str(out)}
    cmd = [sys.executable, "-m", "pytest", "-q", "-m", "integration", "-p", "no:cacheprovider",
           "tests/integration/test_context_economy.py"]
    done = subprocess.run(cmd, cwd=ROOT, env=env)
    if done.returncode != 0 or not out.exists():
        raise SystemExit(f"measurement failed (exit {done.returncode})")
    return json.loads(out.read_text())


def _pct(a: float, b: float) -> str:
    return f"{(b - a) / a:+.1%}" if a else "n/a"


def table(before: dict, after: dict) -> str:
    from analystos.llm.cache import estimate_tokens

    def tok(chars: int) -> int:
        return estimate_tokens("x" * chars) if chars else 0

    lines = ["| scope | calls before -> after | chars before -> after | est. tokens before -> after | change | stable prefix before -> after "
             "| reusable prefix before -> after |", "|---|---:|---:|---:|---:|---:|---:|"]
    for key, label in (("flow", "whole flow (run + redirect + Ask x3)"), ("run_only", "analysis run only")):
        b, a = before[key], after[key]
        lines.append(f"| {label} | {b['calls']} -> {a['calls']} | {b['chars']:,} -> {a['chars']:,} | {tok(b['chars']):,} -> "
                     f"{tok(a['chars']):,} | {_pct(b['chars'], a['chars'])} | {b['flagged_share']:.0%} -> {a['flagged_share']:.0%} "
                     f"| {b['shared_share']:.0%} -> {a['shared_share']:.0%} |")
    lines += ["", "| purpose | calls | chars before -> after | change | stable prefix before -> after | reusable prefix after |",
              "|---|---:|---:|---:|---:|---:|"]
    bp, ap = before["flow"]["per_purpose"], after["flow"]["per_purpose"]
    for p in sorted(set(bp) | set(ap), key=lambda p: -(bp.get(p, {}).get("chars", 0))):
        x, y = bp.get(p, {"calls": 0, "chars": 0, "flagged_share": 0}), ap.get(p, {"calls": 0, "chars": 0, "flagged_share": 0,
                                                                                  "shared_share": 0})
        lines.append(f"| {p} | {x['calls']} -> {y['calls']} | {x['chars']:,} -> {y['chars']:,} | {_pct(x['chars'], y['chars'])} "
                     f"| {x['flagged_share']:.0%} -> {y['flagged_share']:.0%} | {y.get('shared_share', 0):.0%} |")
    lines += ["", "Compiled-context reuse (this process): before " + json.dumps(before.get("context_cache")) + "; after "
              + json.dumps(after.get("context_cache")),
              "Shared context cache (compiled contexts, retrieval), after: " + json.dumps(after.get("context_cache_shared"))]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="measure this tree and write the report here")
    ap.add_argument("--before")
    ap.add_argument("--after")
    ap.add_argument("--md", help="write the comparison table here")
    args = ap.parse_args(argv)
    if args.out:
        report = measure(Path(args.out))
        print(json.dumps({k: v for k, v in report["flow"].items() if k != "per_purpose"}, indent=2))
    if args.before and (args.after or args.out):
        text = table(json.loads(Path(args.before).read_text()), json.loads(Path(args.after or args.out).read_text()))
        print(text)
        if args.md:
            Path(args.md).write_text(text + "\n", encoding="utf-8")
    elif not args.out:
        ap.error("give --out, or --before with --after")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
