"""The ML compute job (P5-01): `run_ml_job(job) -> dict`, pure over snapshot files.

Shaped like `recipes.execute.run_snapshot_job` so an isolated `compute-ml` pool (P7-06) can take it: its
input is JSON (the spec, snapshot hashes, catalog types, caps, the artifact directory), it reads only
content-addressed files and writes only content-addressed files, and a retry gives the same result.

kinds:
* ``prepare``  readiness + split manifest (cheap; the service claims the holdout with its hash)
* ``train``    prepare, then the baseline and a bounded search on the manifest's folds, the frozen
               selection, one holdout read, reproducibility checks, the package and the sealed report
* ``score``    a platform package (hash-verified) over an input snapshot: predictions and rejected rows
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd

from analystos.contracts.work import MLSpec
from analystos.core.ids import stable_hash
from analystos.ml import data as D
from analystos.ml import readiness, splits
from analystos.ml.estimators import search_order
from analystos.ml.evaluation import higher_is_better
from analystos.ml.methods import BY_TASK, code_digest, environment, environment_digest
from analystos.ml.store import MLStore, snapshots

DEFAULT_CAPS = {"max_trials": 12, "max_seconds": 300, "max_rows": 100_000, "max_features": 200}


def run_ml_job(job: dict[str, Any]) -> dict[str, Any]:
    """One job, single-threaded: native BLAS/OpenMP pools are capped at one thread (a resource cap, and on a
    shared worker oversubscribed thread pools are what make fits slow and timings irreproducible)."""
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:  # pragma: no cover - ships with scikit-learn
        return _run(job)
    with threadpool_limits(limits=1):
        return _run(job)


def _run(job: dict[str, Any]) -> dict[str, Any]:
    kind = job.get("kind", "train")
    if kind == "prepare":
        return {k: v for k, v in _prepare(job).items() if not k.startswith("_")}
    if kind == "train":
        return train(job)
    if kind == "score":
        return score(job)
    raise ValueError(f"unknown ML job kind {kind!r}")


def caps_of(job: dict[str, Any]) -> dict[str, int]:
    return {**DEFAULT_CAPS, **{k: int(v) for k, v in (job.get("caps") or {}).items() if v is not None}}


def _prepare(job: dict[str, Any]) -> dict[str, Any]:
    spec = MLSpec.model_validate(job["spec"])
    caps = caps_of(job)
    columns, rows = snapshots(job["artifact_dir"]).get(job["snapshot"])
    fams = D.families(columns, rows, job.get("column_types") or {},
                      {f.column: f.type for f in spec.features if f.type})
    df = D.frame(columns, rows)
    keys = D.row_keys(columns, rows)
    ready = readiness.check(spec, df, fams, as_of=job.get("as_of"), max_rows=caps["max_rows"],
                            max_features=caps["max_features"])
    out: dict[str, Any] = {"status": "refused" if not ready.ok else "ready", "readiness": ready.as_dict(),
                           "families": {f.column: fams.get(f.column) for f in spec.features}}
    if not ready.ok:
        return out
    keep = ready.keep.to_numpy()
    usable = df.loc[keep].reset_index(drop=True)
    ukeys = [k for k, ok in zip(keys, keep, strict=True) if ok]
    if spec.time_column and (spec.task in ("forecast", "anomaly") or
                             (spec.split and spec.split.strategy in ("chronological", "group_chronological"))):
        order = sorted(range(len(usable)), key=lambda i: (D.to_datetime(usable[spec.time_column]).iloc[i], ukeys[i]))
        usable = usable.iloc[order].reset_index(drop=True)
        ukeys = [ukeys[i] for i in order]
    seed = int(spec.seed or 0)
    try:
        split = splits.build(spec, usable, ukeys, dataset_version=job["snapshot"], seed=seed)
    except ValueError as exc:
        out["status"] = "refused"
        out["readiness"]["ok"] = False
        out["readiness"]["problems"].append(f"split: {exc}")
        return out
    if len(split.holdout) < 5 or len(split.train) < 10 or not split.folds:
        out["status"] = "refused"
        out["readiness"]["ok"] = False
        out["readiness"]["problems"].append(
            f"split: too few rows per partition (train {len(split.train)}, holdout {len(split.holdout)}, "
            f"{len(split.folds)} folds); insufficient data is a valid outcome")
        return out
    membership_hash = MLStore(job["artifact_dir"]).put_json("memberships", split.membership)
    out.update(manifest=split.manifest, manifest_hash=split.hash, membership_hash=membership_hash, seed=seed,
               _spec=spec, _df=usable, _fams=fams, _split=split, _keys=ukeys)
    return out


def _better(metric: str, a: float | None, b: float | None) -> bool:
    if a is None:
        return False
    if b is None:
        return True
    return a > b if higher_is_better(metric) else a < b


def train(job: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    prep = _prepare(job)
    public = {k: v for k, v in prep.items() if not k.startswith("_")}
    if prep["status"] != "ready":
        return public
    spec: MLSpec = prep["_spec"]
    if job.get("expected_manifest_hash") and job["expected_manifest_hash"] != prep["manifest_hash"]:
        return {**public, "status": "refused", "error": "the split manifest differs from the one whose holdout was claimed"}
    env_digest = environment_digest()
    if spec.runtime_digest and spec.runtime_digest != env_digest:
        return {**public, "status": "refused", "error": f"the spec pins runtime {spec.runtime_digest[:12]}, this worker "
                                                         f"runs {env_digest[:12]}"}
    caps = caps_of(job)
    split: splits.Split = prep["_split"]
    method = BY_TASK[spec.task]
    learner = method.learner()(spec, prep["_df"], prep["_fams"], split, prep["seed"])
    metric = learner.metric

    def run_trial(tid: int, estimator: str, params: dict[str, Any], role: str) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            cv = learner.cv(estimator, params)
            status, error = ("succeeded" if cv["mean"] is not None else "failed"), None
        except Exception as exc:  # one estimator failing to converge is a failed trial, not a failed experiment
            cv, status, error = {"folds": [], "mean": None}, "failed", f"{type(exc).__name__}: {exc}"[:300]
        return {"trial": tid, "role": role, "estimator": estimator, "params": params, "status": status, "error": error,
                "folds": cv.get("folds"), "mean": cv.get("mean"),
                "extra": {k: v for k, v in cv.items() if k not in ("folds", "mean", "oof")},
                "seconds": round(time.monotonic() - t0, 3)}

    baseline = run_trial(0, spec.baseline, {}, "baseline")
    trials = [baseline]
    order = search_order(spec)
    stopped = None
    for i, (estimator, params) in enumerate(order):
        if i >= caps["max_trials"]:
            stopped = f"trial cap {caps['max_trials']} reached; {len(order) - i} planned trial(s) not run"
            break
        if time.monotonic() - started > caps["max_seconds"]:
            stopped = f"time cap {caps['max_seconds']}s reached after {i} trial(s)"
            break
        trials.append(run_trial(i + 1, estimator, params, "candidate"))
    ok = [t for t in trials[1:] if t["status"] == "succeeded"]
    if baseline["status"] != "succeeded" or not ok:
        return {**public, "status": "failed", "trials": trials,
                "error": "the baseline failed" if baseline["status"] != "succeeded" else "no candidate trial succeeded"}
    best = ok[0]
    for t in ok[1:]:
        if _better(metric, t["mean"], best["mean"]):
            best = t
    # Frozen before the holdout is read: nothing below may change the choice.
    selection = {"trial": best["trial"], "estimator": best["estimator"], "params": best["params"], "cv_mean": best["mean"],
                 "baseline_cv_mean": baseline["mean"], "metric": metric, "manifest_hash": prep["manifest_hash"],
                 "spec_hash": stable_hash(spec.model_dump(mode="json")), "folds": len(split.folds)}
    selection_hash = stable_hash(selection)
    report = learner.evaluate(baseline, best)
    report["holdout"] = {"rows": int(len(split.holdout)), "read_after_selection": selection_hash,
                         "membership": split.manifest["membership"]["holdout"]}
    checks = [
        {"check": "leakage_and_readiness", "outcome": "pass", "reason": "every readiness check passed before splitting"},
        {"check": "baseline_same_splits", "outcome": "pass",
         "reason": f"baseline and every candidate were scored on the manifest's {len(split.folds)} folds and the same "
                   f"{len(split.holdout)} holdout rows"},
        {"check": "holdout_untouched_until_selection", "outcome": "pass",
         "reason": f"selection {selection_hash[:12]} froze before the holdout was read"},
        learner.reproduce(best, report["predictions_hash"]),
    ]
    again = splits.build(spec, prep["_df"], prep["_keys"], dataset_version=job["snapshot"], seed=prep["seed"])
    checks.append({"check": "deterministic_manifest", "outcome": "pass" if again.hash == prep["manifest_hash"] else "fail",
                   "reason": "rebuilding the split from the data and the seed gives the same manifest"
                   if again.hash == prep["manifest_hash"] else "the split is not reproducible"})
    failed_slices = [s for g in report.get("guardrails") or [] for s in g["slices"] if s.get("status") == "fail"]
    checks.append({"check": "slice_guardrails", "outcome": "fail" if failed_slices else
                   ("pass" if spec.guardrails else "not_applicable"),
                   "reason": f"{len(failed_slices)} slice(s) degrade beyond the guardrail" if failed_slices else
                   ("every guarded slice is within its limit" if spec.guardrails else "no guardrail declared")})
    improved = bool((report.get("decision") or {}).get("improved"))
    checks.append({"check": "improvement_over_baseline", "outcome": "pass" if improved else "fail",
                   "reason": (report.get("decision") or {}).get("reason", "")})
    broken = [c for c in checks if c["outcome"] == "fail" and c["check"] not in ("improvement_over_baseline", "slice_guardrails")]
    verdict = "invalid" if broken else "guardrail_failed" if failed_slices else "improved" if improved else "no_improvement"
    report["checks"] = checks
    report["verdict"] = verdict
    report["seal"] = stable_hash({k: v for k, v in report.items() if k != "seal"})
    pkg = learner.package(best, report)
    pkg.update(spec_hash=selection["spec_hash"], manifest_hash=prep["manifest_hash"], dataset_version=job["snapshot"],
               seed=prep["seed"], code_digest=code_digest(), environment=environment(), selection_hash=selection_hash,
               evaluation_seal=report["seal"], entity_keys=list(spec.entity_keys), time_column=spec.time_column)
    package_hash, size = MLStore(job["artifact_dir"]).put_package(pkg)
    return {**public, "status": "succeeded", "verdict": verdict, "trials": trials, "stopped": stopped,
            "caps": {**caps, "trials_run": len(trials) - 1, "trials_planned": len(order)},
            "selection": selection, "selection_hash": selection_hash, "evaluation": report,
            "package": {"hash": package_hash, "bytes": size, "estimator": best["estimator"], "params": best["params"],
                        "schema": pkg.get("schema") or [], "task": spec.task, "forecast": pkg.get("forecast"),
                        "positive": pkg.get("positive"), "classes": pkg.get("classes")},
            "reference_profile": pkg.get("reference_profile") or {}, "code_digest": pkg["code_digest"],
            "environment": pkg["environment"], "environment_digest": env_digest,
            "seconds": round(time.monotonic() - started, 3)}


# ------------------------------------------------------------------------------------ scoring
def score(job: dict[str, Any]) -> dict[str, Any]:
    """Predictions for every input row, or a rejection reason. Output rows are stored as a snapshot."""
    from analystos.ml import anomaly, cluster, tabular

    store = MLStore(job["artifact_dir"])
    package = store.load_package(job["package_hash"])
    keys = list(job.get("entity_keys") or package.get("entity_keys") or [])
    if package["task"] == "forecast":
        cols = ["period", "step", "point", "lo80", "hi80", "lo95", "hi95"]
        rows = [[r[c] for c in cols] for r in package.get("forecast") or []]
        digest = snapshots(job["artifact_dir"]).put(cols, rows)
        return {"status": "succeeded", "result": digest, "columns": cols, "rows_scored": len(rows), "rejected": []}
    columns, rows = snapshots(job["artifact_dir"]).get(job["snapshot"])
    missing = [f["name"] for f in package["schema"] if f["name"] not in columns] + [k for k in keys if k not in columns]
    if missing:
        return {"status": "refused", "error": f"input lacks {', '.join(missing)} (training/scoring feature parity)"}
    df = D.frame(columns, rows)
    if package["task"] == "anomaly" and package.get("time_column") in df.columns:
        df = df.iloc[np.argsort(D.to_datetime(df[package["time_column"]]).to_numpy(), kind="stable")]
    scorer = {"classify": tabular.score_rows, "regress": tabular.score_rows, "cluster": cluster.score_rows,
              "anomaly": anomaly.score_rows}[package["task"]]
    preds, reasons = scorer(package, df)
    key_null = df[keys].isna().any(axis=1) if keys else pd.Series(False, index=df.index)
    reasons = reasons.where(~key_null, (reasons + " entity key is null;").str.strip())
    ok = (reasons == "").to_numpy()
    out_cols = [*keys, "prediction_label", "prediction_value"]
    out_rows = []
    for (_, r), p_label, p_value, good in zip(df.iterrows(), preds["prediction_label"], preds["prediction_value"], ok,
                                             strict=True):
        if good:
            out_rows.append([r[k] for k in keys] + [None if p_label is None else str(p_label),
                                                   None if pd.isna(p_value) else float(p_value)])
    rejected = [{"keys": {k: r[k] for k in keys}, "reason": why}
                for (_, r), why, good in zip(df.iterrows(), reasons, ok, strict=True) if not good]
    digest = snapshots(job["artifact_dir"]).put(out_cols, out_rows)
    return {"status": "succeeded", "result": digest, "columns": out_cols, "rows_scored": len(out_rows), "rejected": rejected,
            "input_rows": len(df)}
