"""Governed classical ML (ADR-0024, P5-01..P5-06).

Pure, deterministic core over immutable snapshots (`jobs.run_ml_job` is the compute job): readiness and
leakage checks, split manifests, a mandatory baseline, a bounded search over allowlisted estimators,
holdout evaluation, packages and model cards. Persistence, approvals, scoring and monitoring live in
`services/ml.py` and `ml/monitoring.py`. Models only propose `MLSpec`s (`ml/propose.py`); code decides
every number.
"""
