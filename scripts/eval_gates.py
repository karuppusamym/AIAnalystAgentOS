"""Evaluation release gates (P7-07, spec v4 §13): run suites and apply the owner's thresholds from
config/eval_gates.yaml. Exit 1 when any gated metric regresses past its threshold.

  python scripts/eval_gates.py                                   # the deterministic CI tiers that need no database
  python scripts/eval_gates.py --tiers grounding                 # one tier
  python scripts/eval_gates.py --tiers ask_fake,ask_off          # needs the control-plane database (integration env)
  python scripts/eval_gates.py --tiers ask_live,analytical_platform_live   # nightly / pre-release (needs a key)
  python scripts/eval_gates.py --out gate-result.json            # the result CI attaches to the build

The result names the thresholds' version and the sha256 of config/eval_gates.yaml, so a release can
show exactly which gates it passed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def main(argv: list[str] | None = None) -> int:
    from evaluation import gates as G

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tiers", default=None, help="comma-separated tiers of config/eval_gates.yaml "
                                                        "(default: deterministic tiers without a database)")
    parser.add_argument("--config", type=Path, default=G.GATES_FILE)
    parser.add_argument("--out", type=Path, default=None, help="write the gate result as JSON")
    parser.add_argument("--require-live", action="store_true", help="a release: a skipped live tier fails")
    args = parser.parse_args(argv)
    gates = G.load(args.config)
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()] if args.tiers else G.deterministic_ci_tiers(gates)
    unknown = [t for t in tiers if t not in gates.tiers]
    if unknown:
        parser.error(f"unknown tier(s) {unknown}; config/eval_gates.yaml has {', '.join(gates.tiers)}")
    result = G.run_gates(tiers, gates, require_live=args.require_live)
    for name, t in result["tiers"].items():
        shown = {k: v for k, v in (t.get("metrics") or {}).items() if k in gates.tiers[name].metrics}
        print(f"[{name}] {t['status']}" + (f" ({t['seconds']}s) {json.dumps(shown)}" if "metrics" in t else f": {t['reason']}"))
        for failure in t.get("failures", []):
            print(f"EVAL GATE FAILED: {failure}", file=sys.stderr)
    print(f"eval gates v{gates.version} (sha256 {gates.digest[:12]}): {'PASS' if result['passed'] else 'FAIL'}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=1, default=str) + "\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
