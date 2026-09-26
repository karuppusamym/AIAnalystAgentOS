"""P5-01/P5-02/P5-04/P5-05 without services: the pure ML job over snapshot files.

Leakage fixtures are refused before any split; grouped and time splits keep entities and time apart;
manifests are deterministic; trial and row caps are hard; baseline and candidate share splits; a null
target abstains; the evaluation report is sealed; packages load only with a matching hash; scoring enforces
feature parity and rejects rows; every method of the pack produces its evidence and re-runs identically."""
from __future__ import annotations

import pickle

import pytest
from evaluation import ml_datasets as F

from analystos.core.errors import Conflict
from analystos.core.ids import stable_hash
from analystos.ml.jobs import run_ml_job
from analystos.ml.store import MLStore, snapshots

pytest.importorskip("sklearn")
pytest.importorskip("statsmodels")


@pytest.fixture
def art(tmp_path):
    return str(tmp_path)


def _put(art, data):
    cols, rows, types = data
    return snapshots(art).put(cols, rows), types


def _job(art, data, spec, kind="train", **extra):
    digest, types = _put(art, data)
    return {"kind": kind, "artifact_dir": art, "snapshot": digest, "column_types": types, "spec": spec,
            "as_of": "2026-01-01T00:00:00+00:00", "caps": extra.pop("caps", {"max_trials": 4, "max_seconds": 120}), **extra}


def _problems(out):
    return " | ".join(out["readiness"]["problems"])


# ------------------------------------------------------------------------------------ leakage and readiness
@pytest.mark.parametrize("change,needle", [
    ({"features": [{"column": "support_calls"}, {"column": "refund_issued"}]}, "target_derived_features"),
    ({"features": [{"column": "support_calls"}, {"column": "churned"}]}, "target_not_a_feature"),
    ({"features": [{"column": "support_calls"}, {"column": "customer_id"}]}, "keys_not_features"),
    ({"features": [{"column": "support_calls"}, {"column": "tenure_months", "available_at": "after_outcome"}]},
     "feature_availability"),
    ({"features": [{"column": "support_calls"}, {"column": "churn_date", "type": "datetime"}],
      "cutoff_column": "snapshot_date", "outcome_time_column": "churn_date"}, "post_outcome_timestamps"),
    ({"features": [{"column": "support_calls"}, {"column": "last_login", "type": "datetime"}],
      "prediction_cutoff": "2025-01-10T00:00:00+00:00"}, "post_cutoff_timestamps"),
])
def test_leakage_fixtures_are_refused_before_any_split(art, change, needle):
    out = run_ml_job(_job(art, F.churn(), F.spec(**change)))
    assert out["status"] == "refused" and needle in _problems(out)
    assert "manifest" not in out and "trials" not in out  # nothing was split or fitted


def test_repeated_entities_need_a_group_aware_split(art):
    data = F.churn(n=120, repeat=3)
    out = run_ml_job(_job(art, data, F.spec()))
    assert out["status"] == "refused" and "group_aware_split" in _problems(out)
    bad = F.spec(group_keys=["customer_id"])
    assert "group_aware_split" in _problems(run_ml_job(_job(art, data, bad)))


def test_label_maturity_excludes_rows_whose_horizon_has_not_closed(art):
    s = F.spec(cutoff_column="snapshot_date", label_horizon=40)
    out = run_ml_job(_job(art, F.churn(), s, kind="prepare", as_of="2025-02-20T00:00:00+00:00"))
    excluded = out["readiness"]["excluded"].get("immature_labels", 0)
    assert 0 < excluded < 400 and out["readiness"]["usable_rows"] == 400 - excluded


def test_insufficient_data_is_a_refusal_not_an_error(art):
    out = run_ml_job(_job(art, F.churn(n=20), F.spec()))
    assert out["status"] == "refused" and "minimum_rows" in _problems(out)


# ------------------------------------------------------------------------------------ splits and manifests
def test_group_split_keeps_every_entity_on_one_side(art):
    s = F.spec(group_keys=["customer_id"], split={"strategy": "group", "holdout_fraction": 0.25})
    out = run_ml_job(_job(art, F.churn(n=150, repeat=3), s, kind="prepare"))
    m = out["manifest"]
    assert out["status"] == "ready" and m["strategy"] == "group" and m["groups"]["overlap"] == 0
    assert m["groups"]["train_groups"] + m["groups"]["holdout_groups"] == 150


def test_chronological_split_boundaries(art):
    s = F.spec(entity_keys=[], time_column="snapshot_date", split={"strategy": "chronological", "embargo_periods": 1})
    out = run_ml_job(_job(art, F.churn(n=300), s, kind="prepare"))
    m = out["manifest"]
    assert out["status"] == "ready", _problems(out)
    assert m["boundaries"]["holdout_from"] > m["boundaries"]["embargo_from"]
    assert m["excluded"].get("embargo", 0) > 0 and len(m["rows"]["folds"]) >= 2


def test_manifests_are_deterministic_and_seed_dependent(art):
    a = run_ml_job(_job(art, F.churn(), F.spec(), kind="prepare"))
    b = run_ml_job(_job(art, F.churn(), F.spec(), kind="prepare"))
    c = run_ml_job(_job(art, F.churn(), F.spec(seed=12), kind="prepare"))
    assert a["manifest_hash"] == b["manifest_hash"] != c["manifest_hash"]
    assert a["manifest_hash"] == stable_hash(a["manifest"])
    assert MLStore(art).get_json("memberships", a["membership_hash"])["holdout"]  # the membership lists are stored


# ------------------------------------------------------------------------------------ search, caps, evaluation
def test_baseline_and_candidates_share_splits_and_the_report_is_sealed(art):
    out = run_ml_job(_job(art, F.churn(), F.spec(guardrails=[{"column": "region", "min_rows": 10, "max_degradation": 0.5}])))
    assert out["status"] == "succeeded" and out["verdict"] == "improved", out["evaluation"]["decision"]
    folds = len(out["manifest"]["rows"]["folds"])
    assert all(len(t["folds"]) == folds for t in out["trials"] if t["status"] == "succeeded")
    assert out["trials"][0]["role"] == "baseline" and out["trials"][0]["estimator"] == "dummy_prior"
    ev = out["evaluation"]
    checks = {c["check"]: c["outcome"] for c in ev["checks"]}
    assert checks["baseline_same_splits"] == checks["holdout_untouched_until_selection"] == "pass"
    assert checks["reproducible_from_manifest"] == checks["deterministic_manifest"] == "pass"
    assert checks["slice_guardrails"] == "pass" and checks["improvement_over_baseline"] == "pass"
    assert ev["holdout"]["read_after_selection"] == out["selection_hash"] == stable_hash(out["selection"])
    assert ev["seal"] == stable_hash({k: v for k, v in ev.items() if k != "seal"})
    assert {"threshold", "calibration", "confusion", "uncertainty", "feature_importance"} <= set(ev)
    assert ev["uncertainty"]["ci_low"] > 0 and ev["feature_importance"]["note"].endswith("not causality")


def test_a_null_target_abstains_and_no_improvement_is_a_valid_result(art):
    out = run_ml_job(_job(art, F.churn(signal=False), F.spec(target="coin_flip")))
    assert out["status"] == "succeeded" and out["verdict"] == "no_improvement"
    assert out["evaluation"]["decision"]["improved"] is False and "no improvement" in out["evaluation"]["decision"]["reason"]
    assert out["package"]["hash"]  # recorded, but a no-improvement result is never promotable (services/ml)


def test_trial_and_row_caps_are_hard(art):
    out = run_ml_job(_job(art, F.churn(), F.spec(), caps={"max_trials": 2, "max_seconds": 120}))
    assert out["caps"]["trials_run"] == 2 and "trial cap 2 reached" in out["stopped"]
    assert len(out["trials"]) == 3  # baseline + 2
    out = run_ml_job(_job(art, F.churn(), F.spec(), caps={"max_rows": 100}))
    assert out["status"] == "refused" and "row_cap" in _problems(out)
    out = run_ml_job(_job(art, F.churn(), F.spec(), caps={"max_trials": 50, "max_seconds": 0}))
    assert out["status"] == "failed" or "time cap" in (out.get("stopped") or "")


def test_a_rerun_from_the_manifest_and_seed_is_identical(art):
    a = run_ml_job(_job(art, F.churn(), F.spec()))
    b = run_ml_job(_job(art, F.churn(), F.spec()))
    assert a["manifest_hash"] == b["manifest_hash"] and a["selection_hash"] == b["selection_hash"]
    assert a["evaluation"]["predictions_hash"] == b["evaluation"]["predictions_hash"]
    assert a["evaluation"]["seal"] == b["evaluation"]["seal"]


def test_regression_reports_error_in_business_units(art):
    s = F.spec(task="regress", target="spend_next", target_unit="EUR",
               features=[{"column": "monthly_spend"}, {"column": "support_calls"}, {"column": "region"}])
    out = run_ml_job(_job(art, F.churn(), s))
    ev = out["evaluation"]
    assert out["verdict"] == "improved" and ev["metric"] == "mae" and ev["residuals"]["candidate"]["unit"] == "EUR"
    assert out["trials"][0]["estimator"] == "dummy_mean"


def test_forecast_backtests_by_horizon_and_reports_interval_coverage(art):
    s = {"task": "forecast", "dataset": {"asset": "shop.weekly"}, "target": "orders", "time_column": "week", "horizon": 6,
         "season_length": 13, "search": {"max_trials": 4}, "seed": 1}
    out = run_ml_job(_job(art, F.weekly_series(), s))
    ev = out["evaluation"]
    assert out["status"] == "succeeded" and out["trials"][0]["estimator"] == "seasonal_naive"
    assert len(ev["candidate"]["abs_error_by_horizon"]) == 6 and len(ev["candidate"]["backtest_abs_error_by_horizon"]) == 6
    assert 0 <= ev["candidate"]["coverage_95"] <= 1 and out["manifest"]["rows"]["holdout"] == 6
    assert len(out["package"]["forecast"]) == 6


def test_cluster_stability_and_random_partition_baseline(art):
    s = {"task": "cluster", "dataset": {"asset": "shop.points"}, "features": [{"column": "x"}, {"column": "y"}],
         "k_range": [2, 4], "search": {"max_trials": 6}, "seed": 2}
    out = run_ml_job(_job(art, F.blobs(), s))
    ev = out["evaluation"]
    assert out["verdict"] == "improved" and ev["stability"]["stable"] and ev["candidate"]["clusters"] == 3
    assert "no causal meaning" in ev["limits"] and out["trials"][0]["estimator"] == "random_partition"


def test_anomaly_thresholds_are_calibrated_on_history(art):
    s = {"task": "anomaly", "dataset": {"asset": "ops.sensor"}, "features": [{"column": "reading"}], "target": "is_incident",
         "time_column": "day", "season_length": 7, "contamination": 0.035, "search": {"max_trials": 3}, "seed": 4}
    out = run_ml_job(_job(art, F.sensor(), s))
    ev = out["evaluation"]
    assert out["status"] == "succeeded" and ev["labels"] is True
    assert ev["candidate"]["threshold_basis"].endswith("quantile of training scores")
    assert "false_alarm_rate" in ev["candidate"] and out["trials"][0]["estimator"] == "robust_z"
    assert out["verdict"] in ("improved", "no_improvement")


# ------------------------------------------------------------------------------------ packages and scoring
def test_packages_load_only_when_their_hash_matches(art):
    out = run_ml_job(_job(art, F.churn(), F.spec()))
    store = MLStore(art)
    h = out["package"]["hash"]
    assert store.load_package(h)["format"] == "analystos.ml.package.v1"
    path = store.package_path(h)
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(Conflict, match="modified"):
        store.load_package(h)
    with pytest.raises(Conflict):  # a pickle placed under someone else's name is refused the same way
        path.write_bytes(pickle.dumps({"format": "analystos.ml.package.v1", "evil": True}))
        store.load_package(h)


def test_scoring_enforces_feature_parity_and_rejects_bad_rows(art):
    out = run_ml_job(_job(art, F.churn(), F.spec()))
    cols, rows, _ = F.churn(n=50, seed=99)
    rows[3][cols.index("tenure_months")] = "twelve"
    ok = run_ml_job({"kind": "score", "artifact_dir": art, "package_hash": out["package"]["hash"],
                     "snapshot": snapshots(art).put(cols, rows)})
    assert ok["status"] == "succeeded" and ok["rows_scored"] == 49 and len(ok["rejected"]) == 1
    assert "tenure_months: not a numeric value" in ok["rejected"][0]["reason"]
    drop = cols.index("support_calls")
    missing = run_ml_job({"kind": "score", "artifact_dir": art, "package_hash": out["package"]["hash"],
                          "snapshot": snapshots(art).put([c for c in cols if c != "support_calls"],
                                                         [[v for i, v in enumerate(r) if i != drop] for r in rows])})
    assert missing["status"] == "refused" and "feature parity" in missing["error"]


def test_mlflow_export_has_the_file_store_layout(art):
    import yaml

    from analystos.ml.mlflow_export import files

    out = run_ml_job(_job(art, F.churn(), F.spec()))
    view = {"id": "mlx_abc123", "definition_key": "churn", "definition_version": 1, "task": "classify",
            "created_at": "2026-09-26T10:00:00+00:00", "finished_at": "2026-09-26T10:01:00+00:00",
            "verdict": out["verdict"], "package_hash": out["package"]["hash"], "created_by": "user:u"}
    records = {"ml_trials": {"trials": out["trials"], "selection": out["selection"]}, "ml_evaluation": {"report": out["evaluation"]},
               "ml_spec": {"spec": F.spec()}, "ml_split_manifest": {"manifest": out["manifest"]},
               "ml_model": {"feature_schema": out["package"]["schema"], "environment": out["environment"]},
               "ml_model_card": {"markdown": "# card"}}
    fs = files(view, records, MLStore(art).package_bytes(out["package"]["hash"]))
    run_dirs = {p.split("/")[2] for p in fs if p.count("/") >= 3}
    assert len(run_dirs) == 1
    run = f"mlruns/1/{run_dirs.pop()}"
    meta = yaml.safe_load(fs[f"{run}/meta.yaml"])
    assert meta["status"] == 3 and meta["experiment_id"] == "1" and yaml.safe_load(fs["mlruns/1/meta.yaml"])["name"]
    line = fs[f"{run}/metrics/cv_roc_auc"].decode().splitlines()[0].split()
    assert len(line) == 3 and float(line[1]) > 0  # "<timestamp> <value> <step>"
    mlmodel = yaml.safe_load(fs[f"{run}/artifacts/model/MLmodel"])
    assert {"sklearn", "python_function", "analystos"} <= set(mlmodel["flavors"])
    model = pickle.loads(fs[f"{run}/artifacts/model/model.pkl"])
    assert hasattr(model, "predict_proba")
    assert fs[f"{run}/params/estimator"].decode() == out["selection"]["estimator"]


def test_model_card_numbers_are_bound_facts(art):
    from analystos.ml.card import build, prose_is_bound

    out = run_ml_job(_job(art, F.churn(), F.spec()))
    card = build({**F.spec(), "baseline": "dummy_prior"}, out, experiment_id="mlx_1", dataset={"asset": "shop.customers",
                                                                                                  "version": "a" * 64})
    assert card["bound"] is True and all(f["source"].startswith("evaluation") for f in card["facts"])
    assert not prose_is_bound(card["markdown"] + "\nThe model lifts retention by 37.25%.", card["facts"])
