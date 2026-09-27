"""P4-08 held-out corpus: score AnalystOS end to end and write the dated evidence report.

  # deterministic component tier (no services; CI runs it as the non-blocking `heldout` gate)
  python scripts/benchmark_heldout.py --tier component
  # the platform tier (Postgres: upload -> staging -> gateway -> real run / recipe run / experiment)
  python scripts/benchmark_heldout.py --tier platform --report auto
  # both, with infrastructure priced at a CPU-hour rate (otherwise reported as unpriced)
  python scripts/benchmark_heldout.py --tier all --cpu-usd-per-hour 0.05 --report auto
  # re-freeze after a reviewed corpus change (bump `version` in corpus.yaml and add a changelog entry first)
  python scripts/benchmark_heldout.py --lock

`--report auto` writes docs/60-delivery/evidence/<date>-heldout-<tiers>-<models>.md (+ .json). Exit 1 when the
corpus no longer matches its lock, or with --check when a proposed threshold is missed.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

KEYS = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY")


def _fmt(x) -> str:
    return "n/a" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))


def _metric_rows(m: dict) -> list[str]:
    cost = m["cost_per_accepted"]
    return [
        f"| tasks (deliver / abstain) | {m['tasks']} ({m['deliver_tasks']} / {m['abstain_tasks']}) |",
        f"| **accepted-output rate** (accepted / deliver tasks) | **{_fmt(m['accepted_output_rate'])}** ({m['accepted']}/{m['deliver_tasks']}) |",
        f"| **confident-wrong** | **{m['confident_wrong']}** |",
        f"| **abstention correctness** (abstain tasks ended as the rubric requires) | **{_fmt(m['abstention_recall'])}** |",
        f"| abstention precision (abstentions that were on abstain tasks) | {_fmt(m['abstention_precision'])} |",
        f"| unnecessary abstentions | {m['unnecessary_abstentions']} |",
        f"| task success (accepted + correct abstentions) / tasks | {_fmt(m['task_success_rate'])} |",
        f"| errors | {m['errors']} |",
        f"| latency p50 / p95 / max (s, per task) | {_fmt(m['latency_seconds']['p50'])} / {_fmt(m['latency_seconds']['p95'])} / "
        f"{_fmt(m['latency_seconds']['max'])} |",
        f"| model calls / tokens / USD | {m['model']['calls']} / {m['model']['tokens']} / {m['model']['usd']} |",
        f"| cost per accepted output: model USD / CPU s / wall s / infrastructure | {_fmt(cost['model_usd'])} / "
        f"{_fmt(cost['cpu_seconds'])} / {_fmt(cost['wall_seconds'])} / {cost['infrastructure_usd']} |",
    ]


def render(runs: dict, args: argparse.Namespace, corpus) -> str:
    from evaluation.heldout.runner import PROPOSED_THRESHOLDS, STATUSES

    now = datetime.now(UTC)
    rev = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    live = any(os.environ.get(k) for k in KEYS)
    lines = [f"# Held-out evaluation (P4-08) — {now:%Y-%m-%d}", "",
             f"Generated {now:%Y-%m-%d %H:%M} UTC by `scripts/benchmark_heldout.py` at `{rev or 'unknown'}` "
             f"(Python {platform.python_version()}). Corpus `evaluation/heldout/corpus.yaml` version {corpus.version} "
             f"(frozen {corpus.frozen}, sha256 `{corpus.digest[:12]}`). Models: "
             + ("**live** (a provider key is set)." if live else "**off** (no provider key: every model purpose takes its rule path)."),
             "", "Every task ends in one status (`evaluation/heldout/runner.py`): accepted, correct_abstention, confident_wrong, "
             "incomplete, unnecessary_abstention, wrong_abstention or error. Rubrics were sealed before the first scored run; "
             "the corpus lock (`corpus.lock.json`) hashes every task's data, reference and rubric.", "",
             "Proposed thresholds (non-blocking `heldout` gate, owner to confirm): "
             + ", ".join(f"{k} {'≥' if 'min' in v else '≤'} {v.get('min', v.get('max'))}" for k, v in PROPOSED_THRESHOLDS.items()) + ".", ""]
    for tier, r in runs.items():
        lines += [f"## Tier: {tier}", "", f"Corpus lock: {'matches' if r.lock_ok else 'MISMATCH ' + ', '.join(r.lock_mismatches)}. "
                  f"Wall time {r.seconds} s.", "", "| Measure | Value |", "|---|---|", *_metric_rows(r.summary["overall"]), "",
                  "| Domain | tasks | accepted | accepted-output rate | confident-wrong | abstention correctness | unnecessary abstentions | errors | p50 s | p95 s |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for d, m in r.summary["by_domain"].items():
            lines.append(f"| {d} | {m['tasks']} | {m['accepted']} | {_fmt(m['accepted_output_rate'])} | {m['confident_wrong']} | "
                         f"{_fmt(m['abstention_recall'])} | {m['unnecessary_abstentions']} | {m['errors']} | "
                         f"{_fmt(m['latency_seconds']['p50'])} | {_fmt(m['latency_seconds']['p95'])} |")
        lines += ["", "### Every task", "", "| Task | family | expect | status | abstained as | seconds | why |", "|---|---|---|---|---|---|---|"]
        for x in r.results:
            expect = x.expect + (f" ({x.abstain_kind})" if x.abstain_kind else "")
            lines.append(f"| {x.id} | {x.family} | {expect} | {x.status} | {x.abstained_as or '—'} | {x.seconds} | "
                         f"{x.reason.replace('|', '/')[:220]} |")
        fails = [x for x in r.results if x.status not in ("accepted", "correct_abstention")]
        lines += ["", f"Failures and non-accepted outcomes ({len(fails)}): "
                  + ("; ".join(f"{x.id} {x.status}" for x in fails) if fails else "none") + ".",
                  "Abstentions: " + ("; ".join(f"{x.id} {x.abstained_as} ({x.status})" for x in r.results if x.produced == "abstained")
                                     or "none") + ".", ""]
        counts = r.summary["overall"]["statuses"]
        lines += ["Status counts: " + ", ".join(f"{s} {counts[s]}" for s in STATUSES) + ".", ""]
    lines += ["## Scope and limits", "",
              f"* Coverage: {len(corpus.tasks)} tasks against the evaluation plan's first-release target of at least 60 "
              "(20 analyst, 15 engineering, 15 ML, 10 unsupported). This version covers ITSM, sales and finance analysis, one "
              "engineering family (recipes: dedupe, late batch, joins, gates) and one ML family (classification, regression, "
              "forecast, leakage and small-data refusals). It does not cover transfer renaming, governance, UX/recovery or "
              "retail/logistics/SaaS-ops domains, and it cannot claim the full suite's capability.",
              "* Synthetic data with planted effects gives known truth; it is not a claim about production data. Planted effects "
              "are sized above the materiality thresholds.",
              "* The component tier has no model in its path; the platform tier ran with "
              + ("a live provider." if live else "no provider key, so it measures the rule path (`off`). A live-model run is a separate dated report."),
              "* Infrastructure cost is reported as CPU and wall seconds; it is priced only when `--cpu-usd-per-hour` is given.",
              "* The paired practitioner baseline (`docs/60-delivery/06-practitioner-baseline-protocol.md`) was **not run**: it "
              "needs qualified human analysts and a blinded scorer. No human-effort or time-saving claim follows from this report.",
              ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from evaluation.heldout import runner as R

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", choices=["component", "platform", "all"], default="component")
    ap.add_argument("--families", default=",".join(R.FAMILIES))
    ap.add_argument("--only", default=None, help="comma-separated task ids (smoke runs)")
    ap.add_argument("--models", choices=["off", "live"], default="off",
                    help="off removes provider keys (rule path); live uses the configured provider")
    ap.add_argument("--cpu-usd-per-hour", type=float, default=None)
    ap.add_argument("--report", default=None, help="markdown path, or `auto` for docs/60-delivery/evidence/")
    ap.add_argument("--check", action="store_true", help="exit 1 when a proposed threshold is missed")
    ap.add_argument("--lock", action="store_true", help="write corpus.lock.json from the current corpus and exit")
    args = ap.parse_args(argv)

    corpus = R.load_corpus()
    if args.lock:
        problems = R.corpus_problems(corpus)
        if problems:
            print("corpus problems: " + "; ".join(problems), file=sys.stderr)
            return 1
        R.LOCK.write_text(json.dumps(R.lock_entries(corpus), indent=1, sort_keys=True) + "\n")
        print(f"locked corpus version {corpus.version}: {len(corpus.tasks)} tasks -> {R.LOCK}")
        return 0
    if args.models == "off":
        for key in KEYS:
            os.environ.pop(key, None)
    os.environ.setdefault("ANALYSTOS_ORCHESTRATOR", "local")

    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    only = {t.strip() for t in args.only.split(",")} if args.only else None
    tiers = ["component", "platform"] if args.tier == "all" else [args.tier]
    runs = {t: R.run(t, families=families, only=only, cpu_usd_per_hour=args.cpu_usd_per_hour, corpus=corpus) for t in tiers}
    problems = []
    for tier, r in runs.items():
        o = r.summary["overall"]
        print(f"[{tier}] tasks={o['tasks']} accepted_output_rate={_fmt(o['accepted_output_rate'])} ({o['accepted']}/{o['deliver_tasks']}) "
              f"confident_wrong={o['confident_wrong']} abstention={_fmt(o['abstention_recall'])} errors={o['errors']} "
              f"p50={_fmt(o['latency_seconds']['p50'])}s p95={_fmt(o['latency_seconds']['p95'])}s lock={'ok' if r.lock_ok else 'MISMATCH'}")
        for x in r.results:
            if x.status not in ("accepted", "correct_abstention"):
                print(f"  {x.id}: {x.status} — {x.reason[:200]}")
        if not r.lock_ok:
            problems.append(f"{tier}: corpus does not match its lock: {', '.join(r.lock_mismatches)}")
        m = R.gate_metrics(r)
        for name, bound in R.PROPOSED_THRESHOLDS.items():
            v = m.get(name)
            if v is None or ("min" in bound and v < bound["min"]) or ("max" in bound and v > bound["max"]):
                problems.append(f"{tier}: {name} {v} misses the proposed {bound}")
    if args.report:
        now = datetime.now(UTC)
        path = (ROOT / "docs" / "60-delivery" / "evidence" / f"{now:%Y-%m-%d}-heldout-{'-'.join(runs)}-{args.models}.md"
                if args.report == "auto" else Path(args.report))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(runs, args, corpus))
        path.with_suffix(".json").write_text(json.dumps({t: R.as_dict(r) for t, r in runs.items()}, indent=1, default=str) + "\n")
        print(f"report: {path}")
    for p in problems:
        print(f"HELD-OUT: {p}", file=sys.stderr)
    lock_broken = any(not r.lock_ok for r in runs.values())
    return 1 if lock_broken or (args.check and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
