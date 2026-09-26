"""`ml.classify` and `ml.regress`: allowlisted scikit-learn pipelines against a dummy baseline.

Preprocessing lives inside each pipeline, so it is fitted on training folds only and applied unchanged
to validation folds, the holdout and batch scoring. The holdout is read once, after the selection froze
(`evaluate`), for the baseline and the candidate on identical rows.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from analystos.contracts.work import MLSpec
from analystos.ml import estimators as E
from analystos.ml import evaluation as V
from analystos.ml.data import model_matrix
from analystos.ml.splits import Split

IMPORTANCE_ROWS = 2000
IMPORTANCE_REPEATS = 3
PROFILE_BINS = 10


def reference_profile(X: pd.DataFrame, schema: list[dict[str, Any]]) -> dict[str, Any]:
    """Training distribution of every feature (drift monitors compare scoring inputs with it)."""
    out: dict[str, Any] = {}
    for f in schema:
        col = X[f["name"]]
        nulls = float(col.isna().mean()) if len(col) else 0.0
        if f["family"] == "categorical":
            counts = col.dropna().astype(str).value_counts(normalize=True)
            top = counts.head(20)
            out[f["name"]] = {"family": "categorical", "null_rate": round(nulls, 6),
                              "shares": {str(k): round(float(v), 6) for k, v in top.items()},
                              "other_share": round(float(max(0.0, 1 - top.sum())), 6)}
        else:
            vals = col.dropna().to_numpy(dtype=float)
            if len(vals) == 0:
                out[f["name"]] = {"family": f["family"], "null_rate": round(nulls, 6), "edges": [], "shares": []}
                continue
            edges = np.unique(np.quantile(vals, np.linspace(0, 1, PROFILE_BINS + 1)[1:-1]))
            shares = np.bincount(np.searchsorted(edges, vals, side="right"), minlength=len(edges) + 1) / len(vals)
            out[f["name"]] = {"family": f["family"], "null_rate": round(nulls, 6), "edges": [float(e) for e in edges],
                              "shares": [round(float(s), 6) for s in shares], "mean": float(vals.mean()),
                              "std": float(vals.std())}
    return out


class Tabular:
    def __init__(self, spec: MLSpec, df: pd.DataFrame, fams: dict[str, str], split: Split, seed: int) -> None:
        self.spec, self.df, self.split, self.seed = spec, df.reset_index(drop=True), split, seed
        self.task = spec.task
        self.metric = spec.metric
        self.schema = [{"name": f.column, "family": f.type or fams.get(f.column, "categorical")} for f in spec.features]
        self.X, reasons = model_matrix(self.df, self.schema)
        self.coerced_to_missing = int((reasons != "").sum())
        if self.task == "classify":
            self.y = self.df[spec.target].map(lambda v: str(v)).to_numpy(dtype=object)
            self.classes = sorted(set(self.y[split.train].tolist()))
            self.positive = V.positive_class(self.classes, self.y[split.train], spec.positive_class)
        else:
            self.y = pd.to_numeric(self.df[spec.target], errors="coerce").to_numpy(dtype=float)
            self.classes, self.positive = [], None
        self._fitted: dict[str, Any] = {}

    # -------------------------------------------------------------- fitting and predicting
    def fit(self, name: str, params: dict[str, Any], rows: np.ndarray) -> Any:
        pipe = E.pipeline(self.task, name, params, self.schema, self.spec, self.seed)
        pipe.fit(self.X.iloc[rows], self.y[rows])
        return pipe

    def predict(self, model: Any, rows: np.ndarray) -> np.ndarray:
        X = self.X.iloc[rows]
        if self.task == "regress":
            return np.asarray(model.predict(X), dtype=float)
        raw = model.predict_proba(X)
        out = np.zeros((len(rows), len(self.classes)))
        for j, c in enumerate(model.classes_):
            if str(c) in self.classes:
                out[:, self.classes.index(str(c))] = raw[:, j]
        return out

    def score(self, pred: np.ndarray, rows: np.ndarray, metric: str | None = None, threshold: float | None = None) -> float:
        metric = metric or self.metric
        if self.task == "regress":
            return V.regress_metric(metric, self.y[rows], pred)
        return V.classify_metric(metric, self.y[rows], pred, self.classes, self.positive, threshold)

    # -------------------------------------------------------------- search
    def cv(self, name: str, params: dict[str, Any], *, keep_oof: bool = False) -> dict[str, Any]:
        scores, oof_rows, oof_pred = [], [], []
        for tr, va in self.split.folds:
            model = self.fit(name, params, tr)
            pred = self.predict(model, va)
            scores.append(V.finite(self.score(pred, va)))
            if keep_oof:
                oof_rows.append(va)
                oof_pred.append(pred)
        valid = [s for s in scores if s is not None]
        out = {"folds": scores, "mean": float(np.mean(valid)) if valid else None}
        if keep_oof:
            out["oof"] = (np.concatenate(oof_rows), np.concatenate(oof_pred))
        return out

    # -------------------------------------------------------------- holdout (read once, after selection froze)
    def evaluate(self, baseline: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
        tr, ho = self.split.train, self.split.holdout
        models = {"baseline": self.fit(baseline["estimator"], baseline["params"], tr),
                  "candidate": self.fit(best["estimator"], best["params"], tr)}
        self._fitted = models
        preds = {k: self.predict(m, ho) for k, m in models.items()}
        report: dict[str, Any] = {"holdout_rows": int(len(ho)), "metric": self.metric,
                                  "higher_is_better": V.higher_is_better(self.metric)}
        threshold = None
        if self.task == "classify" and len(self.classes) == 2:
            rows, oof = self.cv(best["estimator"], best["params"], keep_oof=True)["oof"]
            threshold = V.choose_threshold(self.y[rows], oof[:, self.classes.index(self.positive)], self.positive,
                                           self.spec.error_costs.false_positive, self.spec.error_costs.false_negative)
            report["threshold"] = threshold
        t = threshold["threshold"] if threshold else None
        report["baseline"] = self._metrics(preds["baseline"], ho, t)
        report["candidate"] = self._metrics(preds["candidate"], ho, t)
        cand, base = V.finite(report["candidate"][self.metric]), V.finite(report["baseline"][self.metric])

        def pair(idx: np.ndarray) -> tuple[float, float]:
            return (self.score(preds["candidate"][idx], ho[idx]), self.score(preds["baseline"][idx], ho[idx]))
        boot = V.paired_bootstrap(self.metric, pair, len(ho), seed=self.seed)
        report["uncertainty"] = boot
        report["decision"] = V.decide(self.metric, cand, base, boot, self.spec.min_improvement)
        report["guardrails"], report["slices"] = self._slices(pair, ho)
        if self.task == "classify" and len(self.classes) == 2:
            p = {k: v[:, self.classes.index(self.positive)] for k, v in preds.items()}
            report["calibration"] = {k: V.calibration(self.y[ho], p[k], self.positive) for k in p}
            report["confusion"] = V.confusion(self.y[ho], V.predict_labels(preds["candidate"], self.classes, self.positive, t),
                                              self.positive)
        if self.task == "regress":
            report["residuals"] = {k: V.residuals(self.y[ho], preds[k], self.spec.target_unit) for k in preds}
        report["feature_importance"] = self._importance(models["candidate"], ho, t)
        report["predictions_hash"] = _hash_array(preds["candidate"])
        return report

    def _metrics(self, pred: np.ndarray, rows: np.ndarray, threshold: float | None) -> dict[str, Any]:
        names = ("mae", "rmse", "r2") if self.task == "regress" else \
            (("roc_auc", "log_loss", "balanced_accuracy", "f1", "accuracy", "precision", "recall")
             if len(self.classes) == 2 else ("roc_auc", "log_loss", "balanced_accuracy", "f1", "accuracy"))
        out = {}
        for m in names:
            v = V.finite(self.score(pred, rows, m, threshold))
            out[m] = round(v, 6) if v is not None else None
        return out

    def _slices(self, pair: Any, ho: np.ndarray) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        guard = []
        for g in self.spec.guardrails:
            numeric = self._family(g.column) in ("numeric", "datetime")
            labels, _ = V.slice_values(self.df[g.column].iloc[ho].tolist(), numeric)
            guard.append({"column": g.column, "min_rows": g.min_rows, "max_degradation": g.max_degradation,
                          "slices": V.slice_table(self.metric, labels, pair, min_rows=g.min_rows,
                                                  max_degradation=g.max_degradation)})
        weak = []
        for f in [s for s in self.schema if s["family"] == "categorical"][:3]:
            labels, _ = V.slice_values(self.df[f["name"]].iloc[ho].tolist(), False)
            weak.append({"column": f["name"], "slices": V.slice_table(self.metric, labels, pair, min_rows=20)})
        return guard, weak

    def _family(self, column: str) -> str:
        return next((f["family"] for f in self.schema if f["name"] == column), "categorical") \
            if column in {f["name"] for f in self.schema} else \
            ("numeric" if pd.api.types.is_numeric_dtype(self.df[column]) else "categorical")

    def _importance(self, model: Any, ho: np.ndarray, threshold: float | None) -> dict[str, Any]:
        """Permutation importance on (a seeded sample of) the holdout: how much the objective drops when one
        feature is shuffled. It explains the model's behaviour, not causality."""
        rng = np.random.default_rng(self.seed)
        rows = ho if len(ho) <= IMPORTANCE_ROWS else np.sort(rng.choice(ho, IMPORTANCE_ROWS, replace=False))
        X = self.X.iloc[rows]
        base_pred = self._predict_frame(model, X)
        base = V.finite(self._score_rows(base_pred, rows, threshold))
        out = {}
        if base is None:
            return {"note": "objective undefined on the sample", "features": {}}
        for f in self.schema[:50]:
            drops = []
            for _ in range(IMPORTANCE_REPEATS):
                Xp = X.copy()
                Xp[f["name"]] = rng.permutation(Xp[f["name"]].to_numpy())
                v = V.finite(self._score_rows(self._predict_frame(model, Xp), rows, threshold))
                if v is not None:
                    drops.append(V.gain(self.metric, base, v))
            out[f["name"]] = round(float(np.mean(drops)), 6) if drops else None
        return {"method": "permutation on the holdout", "metric": self.metric,
                "note": "explains model behaviour, not causality", "features": out}

    def _predict_frame(self, model: Any, X: pd.DataFrame) -> np.ndarray:
        if self.task == "regress":
            return np.asarray(model.predict(X), dtype=float)
        raw = model.predict_proba(X)
        out = np.zeros((len(X), len(self.classes)))
        for j, c in enumerate(model.classes_):
            if str(c) in self.classes:
                out[:, self.classes.index(str(c))] = raw[:, j]
        return out

    def _score_rows(self, pred: np.ndarray, rows: np.ndarray, threshold: float | None) -> float:
        return self.score(pred, rows, self.metric, threshold)

    # -------------------------------------------------------------- reproducibility and the package
    def reproduce(self, best: dict[str, Any], expected_hash: str) -> dict[str, Any]:
        model = self.fit(best["estimator"], best["params"], self.split.train)
        got = _hash_array(self.predict(model, self.split.holdout))
        return {"check": "reproducible_from_manifest", "outcome": "pass" if got == expected_hash else "fail",
                "reason": "refitting the frozen selection on the manifest's training rows with the seed gives identical "
                          "holdout predictions" if got == expected_hash else "a refit gave different predictions"}

    def package(self, best: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        return {"task": self.task, "estimator": best["estimator"], "params": best["params"],
                "pipeline": self._fitted["candidate"], "schema": self.schema, "classes": self.classes,
                "positive": self.positive, "threshold": (report.get("threshold") or {}).get("threshold"),
                "target": self.spec.target, "metric": self.metric,
                "reference_profile": reference_profile(self.X.iloc[self.split.train], self.schema)}


def _hash_array(a: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(np.round(np.asarray(a, dtype=float), 10).tobytes()).hexdigest()


def score_rows(package: dict[str, Any], df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Batch scoring with a loaded package: (predictions, rejected-row reasons). The package's fitted
    pipeline runs unchanged; a row whose feature value has the wrong family is rejected, never coerced."""
    X, reasons = model_matrix(df, package["schema"])
    ok = (reasons == "").to_numpy()
    out = pd.DataFrame(index=df.index, data={"prediction_label": None, "prediction_value": np.nan})
    if ok.any():
        model = package["pipeline"]
        if package["task"] == "regress":
            out.loc[ok, "prediction_value"] = np.asarray(model.predict(X[ok]), dtype=float)
        else:
            raw = model.predict_proba(X[ok])
            classes = [str(c) for c in model.classes_]
            pos = package.get("positive")
            if pos in classes and package.get("threshold") is not None and len(classes) == 2:
                p = raw[:, classes.index(pos)]
                neg = next(c for c in classes if c != pos)
                out.loc[ok, "prediction_value"] = p
                out.loc[ok, "prediction_label"] = np.where(p >= package["threshold"], pos, neg)
            else:
                out.loc[ok, "prediction_value"] = raw.max(axis=1)
                out.loc[ok, "prediction_label"] = np.asarray(classes, dtype=object)[raw.argmax(axis=1)]
    return out, reasons
