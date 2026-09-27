# P5-05 — MLflow file-store export opened with a real MLflow client (2026-09-27)

**Row:** P5-05 (tracker). **Question:** does the export of `ml/mlflow_export.py` open in a real MLflow, or only
match the layout the unit test asserts?

## Setup

* A scratch virtualenv outside the project (MLflow is **not** a project dependency): Python 3.11.15,
  `mlflow-skinny` **3.16.1**, scikit-learn 1.9.1 (the version the platform trains with), pandas 3.0.6, numpy 2.4.6
  (`uv pip install mlflow-skinny scikit-learn==1.9.1 ...`, through the session proxy; no install problem).
* The export: the platform's `churn` fixture trained through `analystos.ml.jobs.run_ml_job` (classify, 4 trials,
  selection `logistic C=0.1`, verdict `improved`), written with `ml/mlflow_export.files(...)` to a folder.
* The check: `tests/unit/test_mlflow_client.py` (optional; skips without MLflow). It runs the MLflow client in a child
  interpreter (`ANALYSTOS_MLFLOW_PYTHON=<scratch venv>/bin/python`) inside the unzipped folder.

## What the real client did

| Step | Result |
|---|---|
| `MlflowClient("file:mlruns")` | **Refused by default** in MLflow 3.x: *"The filesystem tracking backend is in maintenance mode"*. Opens with `MLFLOW_ALLOW_FILE_STORE=true`; `mlflow migrate-filestore --source . --target sqlite:///mlflow.db` also migrated it losslessly (1 experiment, 1 run, 6 params, 10 tags, 20 metric points, 16 latest metrics). |
| `search_experiments` / `search_runs` | experiment `1` = `analystos/churn`; one run, status `FINISHED`, run name = the platform experiment id |
| params | `task`, `estimator`, `metric`, `seed`, `split_strategy`, `param.C` — equal to the frozen selection |
| metrics | 16 holdout metrics (`holdout_candidate_*`, `holdout_baseline_*`, `holdout_gain`) equal to the evaluation report; `cv_roc_auc` history = one step per trial (baseline 0.5, then the four candidates' CV means) |
| tags | `analystos.verdict`, `analystos.package_hash`, `analystos.definition`, ... |
| artifacts | `evaluation_report.json`, `model/`, `model_card.md`, `split_manifest.json`, `trials.json`; `model/` holds `MLmodel`, `model.pkl`, `analystos_package.pkl`, `python_env.yaml`, `requirements.txt` |
| `mlflow.sklearn.load_model("runs:/<id>/model")` | loads the scikit-learn `Pipeline`; `predict_proba` on 12 rows equals the platform package's pipeline to 1e-12 |
| `mlflow.pyfunc.load_model(...)` | flavors `analystos`, `python_function`, `sklearn`; `predict` equals the package's labels |

## What the real client rejected, and the fixes

1. **Tags written as the text `None`.** Tags whose value the view does not carry (e.g. `code_digest`) appeared in
   MLflow as the string "None". Fixed: `mlflow_export.files` leaves an unknown tag out.
2. **Integer input columns against the `double` signature.** MLflow's schema enforcement refuses int64 for a column the
   signature declares `double` ("Can not safely convert int64 to float64"). The signature is right: the model's input is
   the platform model matrix (`ml/data.model_matrix`: numeric, boolean and datetime-as-days are floats, categoricals
   strings). Documented in the exporter; the test scores the model matrix.
3. **Relative artifact locations.** `artifact_location: mlruns/1` resolves against the working directory, so the unzipped
   folder must be the working directory (as `mlflow ui` run inside it). An absolute path cannot be known when the zip is
   built; documented in the exporter's docstring.
4. **File store in maintenance mode (MLflow ≥ 3).** Documented: `MLFLOW_ALLOW_FILE_STORE=true` or `mlflow migrate-filestore`.

## Result

`ANALYSTOS_MLFLOW_PYTHON=<scratch>/mlflow-venv/bin/python pytest tests/unit/test_mlflow_client.py` → **1 passed**;
without MLflow → **1 skipped** (reason printed). The tampered-package refusal stays covered by
`tests/unit/test_ml_core.py` (hash-checked `MLStore.load_package`).
