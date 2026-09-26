"""P4-03 method-specific evaluation: per-method null behaviour, power near the materiality threshold and
selection effects (evaluation/method_checks.py), plus the V01 component suite broken down by method.

  # CI (no services, ~20 s): Monte Carlo checks only
  python scripts/benchmark_methods.py --check
  # dated evidence with the V01 per-method breakdown (~3 min)
  python scripts/benchmark_methods.py --v01 --report auto

`--report auto` writes docs/60-delivery/evidence/<date>-method-evaluation.md (+ .json).
Exit code 1 with --check when a threshold (evaluation.method_checks.THRESHOLDS) is missed.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _fmt(x) -> str:
    return "n/a" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))


def render(report, problems: list[str], args: argparse.Namespace) -> str:
    from evaluation.method_checks import ALPHA, MULTIPLES, THRESHOLDS

    now = datetime.now(UTC)
    rev = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    cols = [f"{k:g}x" for k in MULTIPLES]
    lines = [f"# Method-specific evaluation (P4-03) — {now:%Y-%m-%d}", "",
             f"Generated {now:%Y-%m-%d %H:%M} UTC by `scripts/benchmark_methods.py` at `{rev or 'unknown'}` "
             f"(Python {platform.python_version()}), {report.reps} seeded datasets per point, {report.seconds} s. "
             "No services and no model calls: the verdict layer (`skills/stats.py`) the methods use for their primary test.", "",
             "Thresholds: " + ", ".join(f"{k} {v}" for k, v in THRESHOLDS.items()) + f"; nominal α = {ALPHA}.", "",
             "## Null behaviour and power near the materiality threshold", "",
             "Supported rate (the method's verdict: significant *and* the effect clears the materiality threshold) as the true "
             "effect is a multiple k of the threshold. k = 0 is the null.", "",
             "| Method | threshold | design | " + " | ".join(cols) + " | raw p<α at k=0 |",
             "|---|---|---|" + "---|" * len(cols) + "---|"]
    for m in report.methods:
        lines.append(f"| {m.method} | {m.threshold} | {m.design} | " + " | ".join(_fmt(m.power.get(c)) for c in cols)
                     + f" | {_fmt(m.null_raw_p)} |")
    s = report.selection
    lines += ["", "Reading: at k = 1 an effect exactly at the threshold is reported about half the time by construction (the "
              "estimate must clear it); power reaches the 2x target with these sample sizes. The null column is the per-method "
              "false-positive rate of a single test before Benjamini-Hochberg and the second method, which lower it further.", "",
              "## Selection effects", "", f"Design: {s['design']}.", "",
              "| Measure | Value |", "|---|---|",
              f"| quoted top-vs-bottom rate ratio under the null (true 1.0): mean / 95th pct | {s['null_quoted_rate_ratio_mean']} / "
              f"{s['null_quoted_rate_ratio_p95']} |",
              f"| planted: true rate ratio vs mean quoted when supported | {s['planted_true_rate_ratio']} vs "
              f"{_fmt(s['planted_quoted_rate_ratio_mean_when_supported'])} (supported {s['planted_supported_rate']}) |",
              f"| post-hoc top group tested on the **same** data: false-positive rate | **{s['same_data_fpr']}** |",
              f"| post-hoc top group (chosen on one half) tested on the **held-out** half | **{s['held_out_fpr']}** |", "",
              "Reading: choosing the top and bottom groups after looking inflates the quoted ratio even when nothing is there, "
              "and testing a group chosen on the same data rejects far more often than α. This is why P4-03 labels every finding "
              "of an adaptive round a *discovery* and only a held-out partition or a pre-registered re-test on a new data "
              "version may label it *confirmed* (`evidence/confirmation.py`). The quoted ratio is shown with its interval and "
              "the selection procedure in the evidence bundle's method dimension.", ""]
    if report.by_method_v01:
        lines += ["## V01 component suite by method", "",
                  f"Seeds {args.seeds} with planted effects, global-null seeds {args.null_seeds}; ITSM, sales, finance.", "",
                  "| Method | tested | verified | precision | null tests | raw p<α on nulls | FDR under the global null |",
                  "|---|---|---|---|---|---|---|"]
        lines += [f"| {m} | {r['tested']} | {r['verified']} | {_fmt(r['precision'])} | {r['null_tests']} | "
                  f"{_fmt(r['null_raw_rate'])} | {_fmt(r['null_fdr'])} |" for m, r in report.by_method_v01.items()]
        lines.append("")
    lines += ["## Not covered here", "",
              "* pareto, driver_model, cohort_retention and contribution_decomposition have no closed-form threshold sweep here; "
              "their planted/null cases are in `tests/methods` and `tests/unit/test_skills_analysis.py`, and their platform-level "
              "null FDR is the V01 per-method table.",
              "* Power is for the primary verdict; a verified finding additionally needs BH across the run's family and the "
              "second method, so platform power is lower (V01 recall is for material planted effects).",
              "* A live-model tier does not change these numbers: models never compute statistics.", "",
              "## Verdict", "", "PASS — every threshold met." if not problems else "FAIL — " + "; ".join(problems), ""]
    return "\n".join(lines)


def _ints(text: str | None) -> list[int]:
    out: list[int] = []
    for part in (text or "").split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reps", type=int, default=200)
    ap.add_argument("--v01", action="store_true", help="also run the V01 component suite and report it by method")
    ap.add_argument("--seeds", default="1-10")
    ap.add_argument("--null-seeds", default="101-105")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", default=None, help="markdown path, or `auto` for docs/60-delivery/evidence/")
    args = ap.parse_args()

    from evaluation.method_checks import as_dict, check, run

    report = run(args.reps, v01_seeds=_ints(args.seeds) if args.v01 else None,
                 v01_null_seeds=_ints(args.null_seeds) if args.v01 else None)
    problems = check(report)
    for m in report.methods:
        print(f"[{m.method}] null supported={m.null_supported:.3f} raw p<α={m.null_raw_p:.3f} power="
              + " ".join(f"{k}:{v:.2f}" for k, v in m.power.items()))
    print(f"[selection] {report.selection}")
    if args.report:
        path = (ROOT / "docs" / "60-delivery" / "evidence" / f"{datetime.now(UTC):%Y-%m-%d}-method-evaluation.md") \
            if args.report == "auto" else Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(report, problems, args))
        path.with_suffix(".json").write_text(json.dumps(as_dict(report), indent=1, default=str))
        print(f"report: {path}")
    for p in problems:
        print(f"THRESHOLD MISSED: {p}", file=sys.stderr)
    return 1 if (args.check and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
