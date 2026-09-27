"""The `ml` evaluation tier (P5-01..P5-04 behind the P7-07 gates): deterministic, no services.

Runs the pure ML job over seeded fixtures and measures what the governed-ML rules promise:

* leakage_refused_share   every leakage fixture (target-derived, target or key as a feature, a feature known only
                          after the outcome, post-cutoff and post-outcome timestamps, repeated entities under a
                          random split) is refused before anything is split or fitted;
* baseline_parity_share   in every trained case the baseline and every candidate were scored on the same folds and
                          holdout, the holdout was read after the selection froze, and the report is sealed;
* null_abstain_share      a target independent of the features ends as "no improvement" (never promoted);
* signal_detected_share   a real signal (classify, regress, forecast, cluster) ends as "improved";
* deterministic_share     a re-run from the same data, manifest and seed reproduces the manifest, selection and seal;
* tamper_refused_share    a modified package is refused when loaded.
"""
from __future__ import annotations

import tempfile
from typing import Any

from evaluation import ml_datasets as D

LEAKS = [
    {"features": [{"column": "support_calls"}, {"column": "refund_issued"}]},
    {"features": [{"column": "support_calls"}, {"column": "churned"}]},
    {"features": [{"column": "support_calls"}, {"column": "customer_id"}]},
    {"features": [{"column": "support_calls"}, {"column": "tenure_months", "available_at": "after_outcome"}]},
    {"features": [{"column": "support_calls"}, {"column": "churn_date", "type": "datetime"}], "cutoff_column": "snapshot_date",
     "outcome_time_column": "churn_date"},
    {"features": [{"column": "support_calls"}, {"column": "last_login", "type": "datetime"}],
     "prediction_cutoff": "2025-01-10T00:00:00+00:00"},
]


def _job(art: str, data: tuple, spec: dict[str, Any], kind: str = "train", **caps: Any) -> dict[str, Any]:
    from analystos.ml.store import snapshots

    cols, rows, types = data
    return {"kind": kind, "artifact_dir": art, "snapshot": snapshots(art).put(cols, rows), "column_types": types,
            "spec": spec, "as_of": "2026-01-01T00:00:00+00:00", "caps": {"max_trials": 4, "max_seconds": 120, **caps}}


def _parity(out: dict[str, Any]) -> bool:
    from analystos.core.ids import stable_hash

    ev = out["evaluation"]
    folds = len(out["manifest"]["rows"]["folds"])
    checks = {c["check"]: c["outcome"] for c in ev["checks"]}
    return (all(len(t["folds"]) == folds for t in out["trials"] if t["status"] == "succeeded")
            and checks.get("baseline_same_splits") == "pass" and checks.get("holdout_untouched_until_selection") == "pass"
            and ev["holdout"]["read_after_selection"] == out["selection_hash"]
            and ev["seal"] == stable_hash({k: v for k, v in ev.items() if k != "seal"}))


def run() -> dict[str, Any]:
    from analystos.core.errors import Conflict
    from analystos.ml.jobs import run_ml_job
    from analystos.ml.store import MLStore

    art = tempfile.mkdtemp(prefix="aos-ml-gate-")
    churn = D.churn()
    refused = [run_ml_job(_job(art, churn, D.spec(**leak)))["status"] == "refused" for leak in LEAKS]
    refused.append(run_ml_job(_job(art, D.churn(n=120, repeat=3), D.spec()))["status"] == "refused")
    signal = {
        "classify": run_ml_job(_job(art, churn, D.spec())),
        "regress": run_ml_job(_job(art, churn, D.spec(task="regress", target="spend_next", features=[
            {"column": "monthly_spend"}, {"column": "support_calls"}, {"column": "region"}]))),
        "forecast": run_ml_job(_job(art, D.weekly_series(), {"task": "forecast", "dataset": {"asset": "s.w"}, "target": "orders",
                                                              "time_column": "week", "horizon": 6, "season_length": 13,
                                                              "seed": 1})),
        "cluster": run_ml_job(_job(art, D.blobs(), {"task": "cluster", "dataset": {"asset": "s.p"}, "k_range": [2, 4],
                                                     "features": [{"column": "x"}, {"column": "y"}], "seed": 2}, max_trials=6)),
    }
    nulls = [run_ml_job(_job(art, D.churn(signal=False, seed=s), D.spec(target="coin_flip"))) for s in (21, 22, 23)]
    trained = [o for o in [*signal.values(), *nulls] if o.get("status") == "succeeded"]
    again = run_ml_job(_job(art, churn, D.spec()))
    first = signal["classify"]
    same = all(first.get(k) == again.get(k) for k in ("manifest_hash", "selection_hash")) and \
        first["evaluation"]["seal"] == again["evaluation"]["seal"]
    store = MLStore(art)
    path = store.package_path(first["package"]["hash"])
    path.write_bytes(path.read_bytes() + b"x")
    try:
        store.load_package(first["package"]["hash"])
        tamper = False
    except Conflict:
        tamper = True
    return {"leakage_refused_share": sum(refused) / len(refused), "leakage_fixtures": len(refused),
            "baseline_parity_share": sum(_parity(o) for o in trained) / max(len(trained), 1),
            "null_abstain_share": sum(o.get("verdict") == "no_improvement" for o in nulls) / len(nulls),
            "signal_detected_share": sum(o.get("verdict") == "improved" for o in signal.values()) / len(signal),
            "deterministic_share": 1.0 if same else 0.0, "tamper_refused_share": 1.0 if tamper else 0.0,
            "cases": len(trained), "verdicts": {k: o.get("verdict") for k, o in signal.items()}}
