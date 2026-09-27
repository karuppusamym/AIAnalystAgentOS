"""MLflow file-store export of one experiment (P5-05, ADR-0024 decision 3). No MLflow dependency.

Layout (what `mlflow.tracking.MlflowClient(tracking_uri="file:<dir>/mlruns")` reads):

    mlruns/<experiment>/meta.yaml
    mlruns/<experiment>/<run>/meta.yaml, params/<name>, metrics/<name> ("<ms> <value> <step>" lines), tags/<name>
    mlruns/<experiment>/<run>/artifacts/model/{MLmodel, model.pkl, python_env.yaml, requirements.txt}
    mlruns/<experiment>/<run>/artifacts/{evaluation_report.json, model_card.md, split_manifest.json, trials.json}

One MLflow run per platform experiment; each search trial is a step of the `cv_<metric>` metric, the holdout
metrics are single values. The import side (a customer's MLflow) is out of scope.

Opened with a real client (MLflow 3.16, 2026-09-27; `tests/unit/test_mlflow_client.py`): artifact locations are
relative (`mlruns/1`), so open the unzipped folder as the working directory. MLflow 3.x keeps the file store in
maintenance mode: set `MLFLOW_ALLOW_FILE_STORE=true`, or `mlflow migrate-filestore --source . --target sqlite:///...`.
The model's input is the platform model matrix (`ml/data.model_matrix`): numeric, boolean and datetime (days since
epoch) features are doubles, categoricals strings; MLflow's schema check refuses integer columns for a double.
"""
from __future__ import annotations

import io
import json
import pickle
import re
import zipfile
from datetime import datetime
from typing import Any

import yaml

EXPERIMENT_ID = "1"
_SAFE = re.compile(r"[^A-Za-z0-9_.\-/ ]")


def _ms(iso: str | None) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000) if iso else 0


def _name(key: str) -> str:
    return _SAFE.sub("_", key)[:250]


def files(view: dict[str, Any], records: dict[str, Any], package: bytes, *, unpickle: bool = True) -> dict[str, bytes]:
    run_id = view["id"].replace("_", "")[:32].ljust(32, "0")
    start, end = _ms(view.get("created_at")), _ms(view.get("finished_at"))
    exp_dir = f"mlruns/{EXPERIMENT_ID}"
    run_dir = f"{exp_dir}/{run_id}"
    out: dict[str, bytes] = {}

    def put(path: str, data: str | bytes) -> None:
        out[path] = data.encode() if isinstance(data, str) else data

    put(f"{exp_dir}/meta.yaml", yaml.safe_dump({
        "artifact_location": f"mlruns/{EXPERIMENT_ID}", "creation_time": start, "experiment_id": EXPERIMENT_ID,
        "last_update_time": end, "lifecycle_stage": "active", "name": f"analystos/{view['definition_key']}"}))
    put(f"{run_dir}/meta.yaml", yaml.safe_dump({
        "artifact_uri": f"mlruns/{EXPERIMENT_ID}/{run_id}/artifacts", "end_time": end, "entry_point_name": "",
        "experiment_id": EXPERIMENT_ID, "lifecycle_stage": "active", "run_id": run_id, "run_name": view["id"],
        "run_uuid": run_id, "source_name": "", "source_type": 4, "source_version": "", "start_time": start, "status": 3,
        "tags": [], "user_id": view.get("created_by") or ""}))
    trials = (records.get("ml_trials") or {})
    selection = trials.get("selection") or {}
    spec = (records.get("ml_spec") or {}).get("spec") or {}
    params = {"task": view["task"], "estimator": selection.get("estimator"), "metric": selection.get("metric"),
              "seed": spec.get("seed") or 0, "split_strategy": ((records.get("ml_split_manifest") or {}).get("manifest") or {})
              .get("strategy"), **{f"param.{k}": v for k, v in (selection.get("params") or {}).items()}}
    for k, v in params.items():
        put(f"{run_dir}/params/{_name(k)}", json.dumps(v) if isinstance(v, list | dict) else str(v))
    report = (records.get("ml_evaluation") or {}).get("report") or {}
    lines: dict[str, list[str]] = {}
    for step, t in enumerate(trials.get("trials") or []):
        if t.get("mean") is not None:
            lines.setdefault(f"cv_{selection.get('metric')}", []).append(f"{end} {t['mean']} {step}")
    for side in ("candidate", "baseline"):
        for k, v in (report.get(side) or {}).items():
            if isinstance(v, int | float) and not isinstance(v, bool):
                lines.setdefault(f"holdout_{side}_{k}", []).append(f"{end} {v} 0")
    gain = (report.get("decision") or {}).get("gain")
    if isinstance(gain, int | float):
        lines["holdout_gain"] = [f"{end} {gain} 0"]
    for k, v in lines.items():
        put(f"{run_dir}/metrics/{_name(k)}", "\n".join(v) + "\n")
    tags = {"mlflow.runName": view["id"], "mlflow.source.type": "JOB", "mlflow.source.name": "analystos.ml.jobs",
            "analystos.verdict": view.get("verdict"), "analystos.package_hash": view.get("package_hash"),
            "analystos.manifest_hash": view.get("manifest_hash"), "analystos.dataset_version": view.get("dataset_version"),
            "analystos.evaluation_seal": view.get("evaluation_seal"), "analystos.code_digest": view.get("code_digest"),
            "analystos.definition": f"{view['definition_key']}@{view.get('definition_version')}"}
    for k, v in tags.items():
        if v is not None:  # MLflow shows a tag file's text verbatim: an unknown value is left out, not "None"
            put(f"{run_dir}/tags/{_name(k)}", str(v))
    art = f"{run_dir}/artifacts"
    put(f"{art}/evaluation_report.json", json.dumps(records.get("ml_evaluation"), indent=1, default=str))
    put(f"{art}/split_manifest.json", json.dumps(records.get("ml_split_manifest"), indent=1, default=str))
    put(f"{art}/trials.json", json.dumps(trials, indent=1, default=str))
    put(f"{art}/model_card.md", (records.get("ml_model_card") or {}).get("markdown") or "")
    # With the isolated compute-ml pool the control plane never unpickles a package (P7-06): the export then
    # carries the platform package only, without the separate sklearn flavor.
    pkg = pickle.loads(package) if unpickle else {}  # noqa: S301 - the caller verified the platform record and the hash
    model = (records.get("ml_model") or {})
    env = model.get("environment") or {}
    libs = env.get("libraries") or {}
    flavors: dict[str, Any] = {"analystos": {"package": "analystos_package.pkl", "package_hash": view.get("package_hash"),
                                             "task": view["task"], "loads_only_on_matching_hash": True}}
    if pkg.get("pipeline") is not None:
        put(f"{art}/model/model.pkl", pickle.dumps(pkg["pipeline"], protocol=5))
        flavors["python_function"] = {"env": {"virtualenv": "python_env.yaml"}, "loader_module": "mlflow.sklearn",
                                      "model_path": "model.pkl", "predict_fn": "predict",
                                      "python_version": env.get("python")}
        flavors["sklearn"] = {"code": None, "pickled_model": "model.pkl", "serialization_format": "pickle",
                              "sklearn_version": libs.get("scikit-learn")}
    put(f"{art}/model/analystos_package.pkl", package)
    put(f"{art}/model/MLmodel", yaml.safe_dump({
        "artifact_path": "model", "flavors": flavors, "run_id": run_id, "model_uuid": (view.get("package_hash") or "")[:32],
        "utc_time_created": view.get("finished_at"),
        "signature": {"inputs": json.dumps([{"name": f["name"], "type": "double" if f["family"] != "categorical" else "string"}
                                            for f in model.get("feature_schema") or []])}}, sort_keys=False))
    put(f"{art}/model/python_env.yaml", yaml.safe_dump({"python": env.get("python"), "build_dependencies": ["pip"],
                                                        "dependencies": ["-r requirements.txt"]}))
    put(f"{art}/model/requirements.txt", "\n".join(f"{k}=={v}" for k, v in libs.items() if v) + "\n")
    return out


def export_zip(buf: io.BytesIO, view: dict[str, Any], records: dict[str, Any], package: bytes, *,
               unpickle: bool = True) -> None:
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in sorted(files(view, records, package, unpickle=unpickle).items()):
            z.writestr(path, data)
