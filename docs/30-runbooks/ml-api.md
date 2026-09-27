# Governed ML API (P5-01..P5-06, ADR-0024)

Backend reference for the experiment / model / scoring UX (the UI is a later row). All routes are under
`/api/workspaces/{workspace_id}/…`, need a bearer token, and bind every child id to the path's workspace.
Errors use the platform envelope (`core/errors.py`): 422 invalid or refused input, 403 policy, 404, 409
conflict / approval required.

## 1. Definitions (generic API, kinds `ml_spec` and `ml_scoring`)

Specs are authored as definitions (`POST /definitions` draft → `POST /definitions/{id}/publish`). Only a
*published* version trains or scores (a draft only in a workspace with `environment: dev`).

`ml_spec` = `contracts.work.MLSpec`:

| Field | Meaning |
|---|---|
| `task` | `classify` \| `regress` \| `forecast` \| `cluster` \| `anomaly` |
| `dataset` | `{asset: "schema.table", source_id?, version?}`; `version` pins the snapshot hash (changed data refuses) |
| `target`, `positive_class`, `target_unit` | label / series value; classification positive class; unit of regression errors |
| `entity_keys`, `group_keys` | output identity; repeated entities (never cross partitions) |
| `time_column`, `cutoff_column` / `prediction_cutoff`, `outcome_time_column`, `label_horizon` | availability and maturity checks |
| `features[]` | `{column, type?, available_at: cutoff\|known_in_advance\|after_outcome, timestamp_column?}` |
| `split` | `{strategy: chronological\|group\|group_chronological\|random, holdout_fraction, validation_folds, embargo_periods, independence_justification}` (random needs the justification) |
| `preprocessing` | imputation, scaling, one-hot cap (fitted on training folds only) |
| `estimators` | subset of the allowlist (`contracts.work.ESTIMATORS`); the first allowlisted one is the mandatory baseline |
| `search` | `{max_trials, max_seconds, max_rows}`, clamped to `ANALYSTOS_ML_MAX_*` |
| `objective_metric`, `min_improvement`, `error_costs`, `guardrails[]` | selection metric; improvement bar; threshold costs; slice guardrails `{column, min_rows, max_degradation}` |
| `horizon`, `season_length`, `k_range`, `contamination`, `seed`, `runtime_digest` | task parameters, seed, optional environment pin |

`ml_scoring` = `contracts.work.MLScoringSpec`: `{model, model_version_id, package_hash, input: {asset}, output}`.

The ML methods (`method.ml.*`) are disabled per workspace until an owner enables them, or enables
`playbook.train` / `playbook.score` (which bind them). They need the `ml` extra.

## 2. Routes

| Method and path | Body | Result |
|---|---|---|
| `POST /ml/proposals` | `{asset, objective?, target?, task?, features?, estimators?, time_column?, horizon?}` | rules-first `MLSpec` proposal + problems (no model call, nothing stored) |
| `POST /ml/experiments` | `{definition: {key, version} \| "defn_…", max_trials?, max_seconds?}` | 201 experiment view; refusals (leakage, too little data, consumed holdout) are 422/409 with `details.experiment_id` |
| `GET /ml/experiments[?definition=key]` | | experiment views |
| `GET /ml/experiments/{experiment_id}` | | view + `verification` (P7-01 state: verified / void with cause / abstained) |
| `GET /ml/experiments/{experiment_id}/records/{record}` | | `ml_spec`, `ml_split_manifest`, `ml_trials`, `ml_model`, `ml_evaluation` (sealed), `ml_model_card` |
| `GET /ml/experiments/{experiment_id}/mlflow` | | zip of an MLflow file store (`mlruns/…`, `MLmodel`) |
| `GET /ml/models[?name=]` | | model versions (`candidate` \| `challenger` \| `champion` \| `retired`) |
| `POST /ml/models/{version_id}/promote` | `{approval_id?}` | without: `{status: approval_required, approval_id}` (`ml.promote`, hash-bound); with an approved id: `{status: promoted}` |
| `POST /ml/model-names/{name}/rollback` | `{approval_id?}` | same two-step flow (`ml.rollback`); restores the champion the current one replaced |
| `POST /ml/scoring` | `{definition}` | `{status: approval_required, scoring_run_id, approval_id}` or `{status: duplicate, scoring_run_id}` |
| `POST /ml/scoring/{scoring_id}/execute` | `{approval_id?}` | `{status: succeeded, scoring_run}`; writes `ml_<output>` (+ `_rejected`) in the managed output source |
| `GET /ml/scoring`, `GET /ml/scoring/{scoring_id}` | | scoring runs |

Approvals are decided in the generic inbox (`POST /api/approvals/{id}/approve`); separation of duties follows
the workspace policy. A promotion is refused when the experiment did not improve on its baseline or its
verification record is not ACTIVE (a changed dataset snapshot, split, package file or ML code voids it).
Scoring is refused when the pinned version is not the champion or the input's catalog types differ from
training (schema drift).

## 3. Monitors (`POST /monitors`, kinds added by P5-03)

| Kind | Config | Alert |
|---|---|---|
| `ml_drift` | `{model, psi_threshold?=0.2}` | PSI of the latest scored input vs the training profile; drift never claims performance loss |
| `ml_freshness` | `{model, max_age_hours}` | latest successful scoring older than the limit |
| `ml_performance` | `{model, label_asset, label_column, label_horizon_days, key_columns?, metric?, tolerance?=0.05, min_labels?=30}` | matured labels joined with past scores (both via the gateway); loss vs the sealed holdout value beyond the tolerance; too few labels = `waiting` |

## 4. Playbooks

`playbook.train` (origin `{ml_definition: {key, version}}`): `agent.ml_practitioner` proposes (rules in
`off`/`auto` mode), `agent.ml_engineer` trains the published spec (or saves the proposal as a draft when none
is given). `playbook.score` (origin `{scoring_definition: {key, version}}`): plan + approval gate, then the
side-effect step writes the scores. Both are `tested` (never autonomous or scheduled).
