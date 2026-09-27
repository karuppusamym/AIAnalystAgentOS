"""P5-07: the null false-positive rate of ML `improved`, and the power kept on planted signals.

A coin-flip target (`evaluation/heldout/generators.py` `ml_pumps`, variant `null_target`: the label is
independent of every feature) is trained seed after seed through `analystos.ml.jobs.run_ml_job`, exactly as
`evaluation/heldout/runner.py::run_ml_component` does; `null_rate` is the share that ends `improved`. Power is
the share of the planted-signal fixtures (`evaluation/ml.py` and the ML deliver tasks of the held-out corpus)
that still end `improved`.

  python -m evaluation.ml_null --seeds 8000-8199            # the P5-07 measurement (>= 200 seeds)
  python -m evaluation.ml_null --seeds 8000-8199 --unconfirmed   # the pre-P5-07 rule, for comparison

The `ml` gate tier runs `null_metrics()` over a smaller fixed seed range (deterministic, so a gate, not a sample).
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

GATE_SEEDS = range(9000, 9060)
UNCONFIRMED = {"cross_validation": False, "holdout_level": 0.95}


def _train(generator: str, variant: str, seed: int, confirmation: dict[str, Any] | None = None) -> dict[str, Any]:
    from analystos.ml.jobs import run_ml_job
    from analystos.ml.store import snapshots
    from evaluation.heldout import generators as G

    (cols, rows, types), spec = G.ml_task(generator, seed, variant)
    if confirmation is not None:
        spec = {**spec, "confirmation": confirmation}
    with tempfile.TemporaryDirectory(prefix="aos-ml-null-") as art:
        out = run_ml_job({"kind": "train", "artifact_dir": art, "snapshot": snapshots(art).put(cols, rows),
                          "column_types": types, "spec": spec, "as_of": "2026-01-01T00:00:00+00:00",
                          "caps": {"max_trials": 4, "max_seconds": 120}})
    decision = (out.get("evaluation") or {}).get("decision") or {}
    return {"seed": seed, "status": out.get("status"), "verdict": out.get("verdict"), "gain": decision.get("gain"),
            "confirmation": decision.get("confirmation"), "reason": decision.get("reason")}


def _one(args: tuple) -> dict[str, Any]:
    return _train(*args)


def _pool(jobs: list[tuple], workers: int | None) -> list[dict[str, Any]]:
    workers = workers or max(1, min(len(jobs), os.cpu_count() or 1))
    if workers == 1:
        return [_one(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, jobs))


def null_rate(seeds: range | list[int], *, confirmation: dict[str, Any] | None = None,
              workers: int | None = None) -> dict[str, Any]:
    results = _pool([("ml_pumps", "null_target", s, confirmation) for s in seeds], workers)
    improved = [r["seed"] for r in results if r["verdict"] == "improved"]
    holdout_only = [r["seed"] for r in results if (r["confirmation"] or {}).get("passed") is False
                    and "not confirmed" in (r["reason"] or "")]
    return {"null_rate": len(improved) / max(len(results), 1), "null_seeds": len(results), "null_improved": improved,
            "null_refused_by_confirmation": holdout_only,
            "null_trained": sum(r["status"] == "succeeded" for r in results)}


def power(*, confirmation: dict[str, Any] | None = None, workers: int | None = None) -> dict[str, Any]:
    """Planted signals: the ML deliver tasks of the held-out corpus plus the `evaluation/ml.py` fixtures."""
    from evaluation import ml as M

    corpus = [("ml_pumps", "classify", 7301), ("ml_pumps", "regress", 7302), ("ml_calls", "forecast", 7303)]
    got = {f"heldout:{g}:{v}": r["verdict"] for (g, v, _), r in
           zip(corpus, _pool([(g, v, s, confirmation) for g, v, s in corpus], workers), strict=True)}
    got.update({f"fixture:{k}": v for k, v in M.signal_verdicts(confirmation=confirmation).items()})
    return {"power": sum(v == "improved" for v in got.values()) / len(got), "signals": got}


def null_metrics() -> dict[str, Any]:
    """The `ml` gate's null-rate metrics over `GATE_SEEDS` (fixed seeds: the result is deterministic)."""
    out = null_rate(GATE_SEEDS)
    return {k: out[k] for k in ("null_rate", "null_seeds", "null_improved")}


def _seeds(text: str) -> range:
    lo, hi = (int(x) for x in text.split("-"))
    return range(lo, hi + 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds", default="8000-8199")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--unconfirmed", action="store_true", help="measure the pre-P5-07 rule (no confirmation)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    conf = UNCONFIRMED if args.unconfirmed else None
    started = time.monotonic()
    result = {"rule": "unconfirmed (pre-P5-07)" if conf else "confirmed (P5-07 default)", "seeds": args.seeds,
              **null_rate(_seeds(args.seeds), confirmation=conf, workers=args.workers),
              **power(confirmation=conf, workers=args.workers)}
    result["seconds"] = round(time.monotonic() - started, 1)
    text = json.dumps(result, indent=1, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
