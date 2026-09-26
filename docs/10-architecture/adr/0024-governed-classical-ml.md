# ADR-0024 — Governed classical ML: methods, experiment records and batch scoring inside the platform

**Status:** proposed (2026-09-26, spec v4 §10; tracker P5-01..P5-06). Makes the ML scope of the
[workspace spec](../../00-intent/03-workspace-data-team-spec.md) §6 concrete. Source: owner
decision 2026-09-26 (governed classical ML; online serving and full MLOps deferred).

**Context.** The platform has statistical methods (`methods/`), a logistic driver model and a
forecasting skill (`skills/forecast.py`), scikit-learn and statsmodels as dependencies, and a
sandbox that is not a boundary. Neither donor repository has ML code (DataPilot: none; Atlas: see
ADR-0018 inventory). The workspace spec already fixes the rules (split before fitting, group/time-
aware validation, baseline comparison, untouched holdout, model card, approved batch scoring).
This ADR decides *how the platform carries them*.

**Decision.**

1. **ML tasks are methods, not agents.** Each is a capability with a manifest, a typed `MLSpec`
   and a deterministic implementation: `ml.forecast` (statsmodels ETS/ARIMA + seasonal naive
   baseline, rolling-origin backtest), `ml.classify` and `ml.regress` (scikit-learn pipelines from an
   allowlisted estimator set: linear/logistic, gradient-boosted trees, random forest; a dummy
   baseline always), `ml.cluster` (k-means/GMM with stability check), `ml.anomaly` (seasonal
   residual + isolation forest, thresholds calibrated on history). They run in the `compute-ml`
   pool (ADR-0022) on an immutable snapshot artifact.
2. **What models do:** propose the target/horizon mapping from the brief, name candidate features
   from the semantic model, pick among allowlisted estimators, and write the model-card prose from
   bound facts. **What code does:** leakage checks (feature availability vs prediction cutoff,
   target-derived columns, post-outcome timestamps), split construction, fitting, search within the
   trial budget, evaluation, guardrail decisions. No model output becomes a metric value.
3. **Experiment record = artifacts, MLflow-compatible export.** An experiment is a versioned
   artifact set: `MLSpec`, split manifest (row membership hashes), per-trial params/metrics, the
   chosen pipeline package, evaluation report, model card. The registry is the existing artifact
   store (ADR-0011 workspace), not an MLflow server. An exporter writes the MLflow file-store layout
   (and an MLflow `MLmodel` for the package) so a customer MLflow can import runs; the import side is
   out of scope. Packages load only when their content hash matches a platform-produced record
   (no arbitrary pickle upload).
4. **Verification.** An ML result is `predictive-evaluated` evidence (P4-03 dimension) only when:
   baseline and candidate share splits, the holdout was untouched until selection froze, guardrail
   slices pass, and the result reproduces from the split manifest and seed. The verdict's
   fingerprint (ADR-0020) includes the dataset version, split manifest hash, estimator package
   hash and code digest. "No improvement over baseline" is a valid, reportable result.
5. **Promotion and scoring.** A champion is promoted by a hash-bound approval. Batch scoring is a
   published definition (ADR-0021) that pins the model package and input recipe, writes through the
   managed writer with the destination identity (ADR-0011 workspace), and records rejected rows.
   Retraining creates a challenger; it never replaces the champion without a new approval.
6. **Monitoring** reuses monitors (ADR-0009): input drift and freshness as data monitors; predictive
   error as a delayed-label monitor that evaluates when labels mature. Drift alone opens an
   investigation; it doesn't claim performance loss.
7. **Deferred:** online serving endpoints, feature store, deep learning, GPU pools, AutoML beyond
   the bounded search, causal-effect estimation outside a declared experiment design.

**Consequences.** Data scientists get reproducible, reviewed models inside the same governance
and evidence model as analysts, with an exit path to their MLflow. The estimator allowlist and
trial budgets limit what the platform will fit; widening either is a capability version change
with its own evaluation, not a configuration toggle.
