"""P5-05: the MLflow file-store export opens with a real MLflow client: the run is listed, its params, metrics,
trial history and tags read back, and the exported scikit-learn model loads (sklearn and pyfunc flavors) and
predicts exactly what the platform package predicts.

Optional: mlflow is not a project dependency. The check runs in a child interpreter: this one when mlflow is
importable, else ``ANALYSTOS_MLFLOW_PYTHON`` (an interpreter with mlflow and the same scikit-learn, e.g. a
scratch venv); without either it skips. The export's artifact locations are relative, so the child runs in the
unzipped folder, as a person opening the export would."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest
from evaluation import ml_datasets as F

from analystos.ml import data as D
from analystos.ml.data import model_matrix
from analystos.ml.jobs import run_ml_job
from analystos.ml.mlflow_export import files
from analystos.ml.store import MLStore, snapshots

pytest.importorskip("sklearn")

CHECK = textwrap.dedent("""
    import json, sys
    import mlflow
    import pandas as pd
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri="file:mlruns")
    mlflow.set_tracking_uri("file:mlruns")
    [exp] = [e for e in client.search_experiments() if e.experiment_id == "1"]
    [run] = client.search_runs([exp.experiment_id])
    metric = next(k for k in run.data.metrics if k.startswith("cv_"))
    rows = pd.DataFrame(json.load(open(sys.argv[1])))
    model = mlflow.sklearn.load_model(f"runs:/{run.info.run_id}/model")
    pyfunc = mlflow.pyfunc.load_model(f"runs:/{run.info.run_id}/model")
    print(json.dumps({
        "mlflow": mlflow.__version__, "experiment": exp.name, "run_id": run.info.run_id, "run_name": run.info.run_name,
        "status": run.info.status, "params": run.data.params, "metrics": run.data.metrics, "tags": run.data.tags,
        "history": [[m.step, m.value] for m in client.get_metric_history(run.info.run_id, metric)],
        "artifacts": sorted(a.path for a in client.list_artifacts(run.info.run_id)),
        "model_artifacts": sorted(a.path for a in client.list_artifacts(run.info.run_id, "model")),
        "flavors": sorted(pyfunc.metadata.flavors), "proba": model.predict_proba(rows).tolist(),
        "pyfunc": [str(v) for v in pyfunc.predict(rows)]}))
""")


def _interpreter() -> str:
    if importlib.util.find_spec("mlflow") is not None:
        return sys.executable
    other = os.environ.get("ANALYSTOS_MLFLOW_PYTHON")
    if other and os.path.exists(other):
        return other
    pytest.skip("mlflow is not installed (optional; set ANALYSTOS_MLFLOW_PYTHON to an interpreter that has it)")


def test_the_export_opens_with_a_real_mlflow_client(tmp_path):
    python = _interpreter()
    art = tmp_path / "art"
    art.mkdir()
    cols, rows, types = F.churn()
    out = run_ml_job({"kind": "train", "artifact_dir": str(art), "snapshot": snapshots(str(art)).put(cols, rows),
                      "column_types": types, "spec": F.spec(), "as_of": "2026-01-01T00:00:00+00:00",
                      "caps": {"max_trials": 4, "max_seconds": 120}})
    assert out["status"] == "succeeded"
    view = {"id": "mlx_realclient1", "definition_key": "churn", "definition_version": 3, "task": "classify",
            "created_at": "2026-09-27T10:00:00+00:00", "finished_at": "2026-09-27T10:01:00+00:00",
            "verdict": out["verdict"], "package_hash": out["package"]["hash"], "created_by": "user:u",
            "manifest_hash": out["manifest_hash"], "evaluation_seal": out["evaluation"]["seal"]}
    records = {"ml_trials": {"trials": out["trials"], "selection": out["selection"]},
               "ml_evaluation": {"report": out["evaluation"]}, "ml_spec": {"spec": F.spec()},
               "ml_split_manifest": {"manifest": out["manifest"]},
               "ml_model": {"feature_schema": out["package"]["schema"], "environment": out["environment"]},
               "ml_model_card": {"markdown": "# card"}}
    package = MLStore(str(art)).package_bytes(out["package"]["hash"])
    export = tmp_path / "export"
    for path, data in files(view, records, package).items():
        (export / path).parent.mkdir(parents=True, exist_ok=True)
        (export / path).write_bytes(data)
    # the model's input is the platform model matrix (the signature: numeric/boolean/datetime as double)
    matrix, _ = model_matrix(D.frame(cols, rows[:12]), out["package"]["schema"])
    (tmp_path / "rows.json").write_text(matrix.to_json(orient="records"))
    env = {**os.environ, "MLFLOW_ALLOW_FILE_STORE": "true", "MLFLOW_DISABLE_AGENT_HINT": "1",
           "MLFLOW_TRACKING_URI": "file:mlruns"}
    proc = subprocess.run([python, "-c", CHECK, str(tmp_path / "rows.json")], cwd=export, env=env, capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    sel = out["selection"]
    assert got["experiment"] == "analystos/churn" and got["run_name"] == view["id"] and got["status"] == "FINISHED"
    assert got["params"]["estimator"] == sel["estimator"] and got["params"]["metric"] == sel["metric"] == "roc_auc"
    assert got["params"]["task"] == "classify" and got["params"]["split_strategy"] == out["manifest"]["strategy"]
    assert got["metrics"]["holdout_gain"] == pytest.approx(out["evaluation"]["decision"]["gain"])
    assert got["metrics"]["holdout_candidate_roc_auc"] == pytest.approx(out["evaluation"]["candidate"]["roc_auc"])
    assert [v for _, v in got["history"]] == pytest.approx([t["mean"] for t in out["trials"] if t["mean"] is not None])
    assert got["tags"]["analystos.verdict"] == out["verdict"] and got["tags"]["analystos.package_hash"] == out["package"]["hash"]
    assert "analystos.code_digest" not in got["tags"]  # not in the view: left out rather than written as "None"
    assert {"evaluation_report.json", "model", "model_card.md", "split_manifest.json", "trials.json"} <= set(got["artifacts"])
    assert {"model/MLmodel", "model/model.pkl", "model/analystos_package.pkl"} <= set(got["model_artifacts"])
    assert {"sklearn", "python_function", "analystos"} <= set(got["flavors"])
    # the model MLflow loads predicts exactly what the platform's own package predicts
    pipe = MLStore(str(art)).load_package(out["package"]["hash"])["pipeline"]
    assert np.allclose(got["proba"], pipe.predict_proba(matrix), rtol=0, atol=1e-12)
    assert got["pyfunc"] == [str(v) for v in pipe.predict(matrix)]
