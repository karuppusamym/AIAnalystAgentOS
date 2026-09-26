"""ML monitors (P5-03, ADR-0024 decision 6), evaluated by `services/monitors.evaluate_monitor`.

* ``ml_drift``        the latest scored input vs the champion's training reference profile: population
                      stability index per feature. Drift opens an alert (and, by policy, an investigation);
                      it never claims a loss of performance.
* ``ml_freshness``    hours since the latest successful scoring run (and since the input table was staged).
* ``ml_performance``  delayed labels: predictions old enough for their labels to have matured, joined with
                      the label table (both read through the query gateway), scored with the training metric
                      and compared with the sealed holdout value. Too few matured labels is "waiting", not ok.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import utcnow

KINDS = ("ml_drift", "ml_freshness", "ml_performance")
PSI_WARNING, PSI_CRITICAL = 0.2, 0.5


def validate(kind: str, config: dict[str, Any]) -> None:
    if not config.get("model"):
        raise InvalidInput(f"{kind} needs config.model (the model name)")
    if kind == "ml_freshness" and not isinstance(config.get("max_age_hours"), int | float):
        raise InvalidInput("ml_freshness needs a numeric config.max_age_hours")
    if kind == "ml_performance":
        missing = [k for k in ("label_asset", "label_column", "label_horizon_days") if not config.get(k)]
        if missing:
            raise InvalidInput(f"ml_performance needs config.{', config.'.join(missing)}")


def _champion(session: Session, workspace_id: str, name: str) -> Any:
    from analystos.services.ml import current_champion

    mv = current_champion(session, workspace_id, name)
    if mv is None:
        raise NotFound(f"model {name} has no champion to monitor")
    return mv


def psi(expected: list[float], actual: list[float]) -> float:
    e = np.clip(np.asarray(expected, dtype=float), 1e-4, None)
    a = np.clip(np.asarray(actual, dtype=float), 1e-4, None)
    return float(np.sum((a - e) * np.log(a / e)))


def feature_psi(profile: dict[str, Any], values: list[Any]) -> dict[str, Any]:
    from analystos.ml.data import to_number

    import pandas as pd

    s = pd.Series(values, dtype=object)
    nulls = float(s.isna().mean()) if len(s) else 0.0
    if profile.get("family") == "categorical":
        shares = profile.get("shares") or {}
        present = s.dropna().astype(str)
        cur = present.value_counts(normalize=True)
        cats = list(shares)
        exp = [*[shares[c] for c in cats], profile.get("other_share", 0.0)]
        act = [*[float(cur.get(c, 0.0)) for c in cats], float(max(0.0, 1 - sum(float(cur.get(c, 0.0)) for c in cats)))]
    else:
        nums = to_number(s)[0].dropna().to_numpy(dtype=float)
        edges = profile.get("edges") or []
        if not len(nums) or not profile.get("shares"):
            return {"psi": None, "null_rate": round(nulls, 4), "reason": "no values"}
        exp = profile["shares"]
        act = list(np.bincount(np.searchsorted(np.asarray(edges), nums, side="right"), minlength=len(edges) + 1) / len(nums))
    return {"psi": round(psi(exp, act), 6), "null_rate": round(nulls, 4),
            "training_null_rate": profile.get("null_rate")}


def _drift(session: Session, monitor: Any) -> dict[str, Any]:
    from analystos.core.config import get_settings
    from analystos.db.models import Artifact, MLExperiment, MLScoringRun
    from analystos.ml.store import snapshots

    mv = _champion(session, monitor.workspace_id, monitor.config["model"])
    exp = session.get(MLExperiment, mv.experiment_id)
    pkg = session.get(Artifact, (exp.artifacts or {}).get("ml_model"))
    profile = ((pkg.content if pkg else {}) or {}).get("reference_profile") or {}
    run = session.scalar(select(MLScoringRun).where(MLScoringRun.workspace_id == monitor.workspace_id,
                                                    MLScoringRun.model_version_id == mv.id, MLScoringRun.status == "succeeded",
                                                    MLScoringRun.input_version.is_not(None))
                         .order_by(MLScoringRun.created_at.desc()).limit(1))
    if run is None:
        return {"alert": False, "state": "waiting", "message": f"{mv.name} v{mv.version} has not scored an input yet",
                "model_version_id": mv.id}
    columns, rows = snapshots(get_settings().artifact_dir).get(run.input_version)
    threshold = float(monitor.config.get("psi_threshold", PSI_WARNING))
    out = {}
    for name, prof in profile.items():
        if name in columns:
            i = columns.index(name)
            out[name] = feature_psi(prof, [r[i] for r in rows])
    drifted = {k: v for k, v in out.items() if (v.get("psi") or 0) >= threshold}
    worst = max((v.get("psi") or 0 for v in out.values()), default=0.0)
    message = (f"input drift on {', '.join(sorted(drifted))} (max PSI {worst:.3f} >= {threshold}); drift alone does not show "
               "a loss of performance: investigate the inputs, and judge performance when labels mature"
               if drifted else f"no input drift (max PSI {worst:.3f} < {threshold})")
    return {"alert": bool(drifted), "severity": "critical" if worst >= PSI_CRITICAL else "warning", "direction": "input drift",
            "message": message, "features": out, "drifted": sorted(drifted), "max_psi": round(worst, 6),
            "scoring_run_id": run.id, "period": run.input_version[:16], "model_version_id": mv.id}


def _freshness(session: Session, monitor: Any) -> dict[str, Any]:
    from analystos.db.models import MLScoringRun

    mv = _champion(session, monitor.workspace_id, monitor.config["model"])
    run = session.scalar(select(MLScoringRun).where(MLScoringRun.workspace_id == monitor.workspace_id,
                                                    MLScoringRun.model_version_id == mv.id, MLScoringRun.status == "succeeded")
                         .order_by(MLScoringRun.finished_at.desc()).limit(1))
    limit = float(monitor.config["max_age_hours"])
    if run is None or run.finished_at is None:
        return {"alert": True, "severity": "warning", "direction": "stale scores", "period": "never",
                "message": f"{mv.name} v{mv.version} has never scored", "model_version_id": mv.id}
    finished = run.finished_at if run.finished_at.tzinfo else run.finished_at.replace(tzinfo=utcnow().tzinfo)
    age = (utcnow() - finished).total_seconds() / 3600
    stale = age > limit
    return {"alert": stale, "severity": "warning", "direction": "stale scores", "age_hours": round(age, 3),
            "max_age_hours": limit, "scoring_run_id": run.id, "period": run.id, "model_version_id": mv.id,
            "message": (f"latest scores are {age:.1f}h old (limit {limit:g}h)" if stale else
                        f"scores are {age:.1f}h old (within {limit:g}h)")}


def _read(owner: Any, session: Session, workspace_id: str, asset: str, columns: list[str], *, where: str | None,
          source_id: str | None, purpose: str) -> Any:
    from sqlglot import exp

    from analystos.governance.policy import resolve_scope
    from analystos.recipes.compiler import col
    from analystos.runtime.context import default_gateway

    scope = resolve_scope(session, session.merge(owner), workspace_id, minimum_role="viewer")
    if asset not in scope.assets:
        raise InvalidInput(f"{asset} is not in the monitor owner's scope")
    source_id = source_id or scope.asset_sources.get(asset)
    schema, table = asset.split(".", 1)
    stmt = exp.select(*[col(c) for c in columns]).from_(exp.Table(this=exp.to_identifier(table, quoted=True),
                                                                   db=exp.to_identifier(schema, quoted=True)))
    if where:
        stmt = stmt.where(where)
    runner = default_gateway().run_sql_for(scope, actor=f"user:{owner.id}", source_id=source_id)
    return runner(stmt.sql(dialect=scope.source_dialects.get(source_id, "postgres")), purpose=purpose, use_cache=False)


def _performance(session: Session, owner: Any, monitor: Any) -> dict[str, Any]:
    from analystos.contracts.work import MLSpec
    from analystos.db.models import MLExperiment, MLScoringRun
    from analystos.ml import evaluation as V

    cfg = monitor.config
    mv = _champion(session, monitor.workspace_id, cfg["model"])
    exp = session.get(MLExperiment, mv.experiment_id)
    spec = MLSpec.model_validate(exp.spec)
    keys = list(cfg.get("key_columns") or spec.entity_keys)
    if not keys:
        raise InvalidInput("ml_performance needs entity keys to join predictions with labels")
    runs = list(session.scalars(select(MLScoringRun).where(MLScoringRun.workspace_id == monitor.workspace_id,
                                                           MLScoringRun.model_version_id == mv.id,
                                                           MLScoringRun.status == "succeeded")))
    cutoff = utcnow() - timedelta(days=float(cfg["label_horizon_days"]))
    matured = [r for r in runs if r.finished_at and (r.finished_at if r.finished_at.tzinfo else
                                                     r.finished_at.replace(tzinfo=cutoff.tzinfo)) <= cutoff]
    metric = cfg.get("metric") or spec.metric
    reference = ((mv.metrics or {}).get("candidate") or {}).get(metric)
    base = {"model_version_id": mv.id, "metric": metric, "reference": reference, "matured_scoring_runs": len(matured)}
    if not matured:
        return {**base, "alert": False, "state": "waiting",
                "message": f"no predictions older than the {cfg['label_horizon_days']}-day label horizon yet"}
    table = matured[0].output_table
    ids = ",".join(f"'{r.id}'" for r in matured)
    preds = _read(owner, session, monitor.workspace_id, table, [*keys, "prediction_label", "prediction_value", "aos_run_id"],
                  where=f"aos_run_id IN ({ids})", source_id=matured[0].output_source_id, purpose=f"ml.monitor:{monitor.id}")
    labels = _read(owner, session, monitor.workspace_id, cfg["label_asset"], [*keys, cfg["label_column"]], where=None,
                   source_id=cfg.get("label_source_id"), purpose=f"ml.monitor:{monitor.id}")
    truth = {tuple(str(v) for v in r[:len(keys)]): r[len(keys)] for r in labels.rows if r[len(keys)] is not None}
    pairs = [(truth[k], r) for r in preds.rows if (k := tuple(str(v) for v in r[:len(keys)])) in truth]
    min_labels = int(cfg.get("min_labels", 30))
    base["query_ids"] = [preds.query_id, labels.query_id]
    if len(pairs) < min_labels:
        return {**base, "alert": False, "state": "waiting", "labels": len(pairs),
                "message": f"{len(pairs)} matured label(s), need {min_labels} to judge performance"}
    y = np.asarray([str(t) for t, _ in pairs], dtype=object)
    if mv.task == "regress":
        value = V.regress_metric(metric, np.asarray([float(t) for t, _ in pairs]),
                                 np.asarray([float(r[len(keys) + 1]) for _, r in pairs]))
    elif metric == "roc_auc" and mv.task == "classify":
        from sklearn.metrics import roc_auc_score

        from analystos.db.models import Artifact

        art = session.get(Artifact, (exp.artifacts or {}).get("ml_model"))
        pos = str(((art.content if art else {}) or {}).get("positive") or spec.positive_class or "True")
        truth_pos = y == pos
        value = float(roc_auc_score(truth_pos, [float(r[len(keys) + 1]) for _, r in pairs])) if 0 < truth_pos.sum() < len(y) \
            else float("nan")
    else:
        value = float(np.mean(y == np.asarray([str(r[len(keys)]) for _, r in pairs], dtype=object)))
        metric = "accuracy"
    tolerance = float(cfg.get("tolerance", 0.05))
    value = V.finite(value)
    if value is None or not isinstance(reference, int | float):
        return {**base, "alert": False, "state": "waiting", "labels": len(pairs), "value": value,
                "message": "the metric is undefined on the matured labels (e.g. one class only)"}
    loss = -V.gain(metric, value, float(reference))
    degraded = loss > tolerance
    return {**base, "alert": degraded, "severity": "critical" if loss > 2 * tolerance else "warning",
            "direction": "performance loss", "labels": len(pairs), "value": round(value, 6), "loss": round(loss, 6),
            "tolerance": tolerance, "period": ",".join(sorted(r.id for r in matured))[:64],
            "message": (f"{metric} on {len(pairs)} matured labels is {value:.4f} vs {reference:.4f} at evaluation "
                        f"(loss {loss:.4f} > {tolerance}); train a challenger" if degraded else
                        f"{metric} on {len(pairs)} matured labels is {value:.4f} (evaluation {reference:.4f})")}


def evaluate(session: Session, owner: Any, monitor: Any) -> dict[str, Any]:
    if monitor.kind == "ml_drift":
        return _drift(session, monitor)
    if monitor.kind == "ml_freshness":
        return _freshness(session, monitor)
    if monitor.kind == "ml_performance":
        return _performance(session, owner, monitor)
    raise InvalidInput(f"unknown ML monitor kind {monitor.kind}")


