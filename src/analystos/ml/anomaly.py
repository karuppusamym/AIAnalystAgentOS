"""`ml.anomaly`: seasonal residuals and isolation forests against a robust-z baseline.

Every detector turns rows into a score; its alarm threshold is calibrated on the training history as the
(1 - contamination) quantile of the training scores, never on the rows it is judged on. With 0/1 labels
(`target`) the detectors are compared on precision, recall, F1 and the false-alarm rate of the backtest
folds and the holdout. Without labels a detector's quality cannot be measured: the result reports alarm
rates and abstains.
"""
from __future__ import annotations

import hashlib
import warnings
from typing import Any

import numpy as np
import pandas as pd

from analystos.contracts.work import MLSpec
from analystos.ml import evaluation as V
from analystos.ml.data import model_matrix, to_bool
from analystos.ml.splits import Split
from analystos.ml.tabular import reference_profile


class _Detector:
    def __init__(self, name: str, params: dict[str, Any], seed: int, season: int) -> None:
        self.name, self.params, self.seed, self.season = name, params, seed, season

    def fit(self, Z: np.ndarray, contamination: float) -> _Detector:
        if self.name == "isolation_forest":
            from sklearn.ensemble import IsolationForest

            self.model = IsolationForest(n_estimators=int(self.params.get("n_estimators", 100)),
                                         max_samples=self.params.get("max_samples", "auto"), random_state=self.seed, n_jobs=1)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # max_samples above the row count is clamped by scikit-learn
                self.model.fit(Z)
        else:
            self.median = np.nanmedian(Z, axis=0)
            mad = np.nanmedian(np.abs(Z - self.median), axis=0) * 1.4826
            self.mad = np.where(mad > 0, mad, np.nanstd(Z, axis=0) + 1e-12)
        self.threshold = float(np.quantile(self.score(Z), 1 - contamination))
        return self

    def score(self, Z: np.ndarray) -> np.ndarray:
        if self.name == "isolation_forest":
            return -self.model.score_samples(Z)
        z = np.abs((Z - self.median) / self.mad)
        return np.nan_to_num(np.nanmax(z, axis=1), nan=0.0)

    def flag(self, Z: np.ndarray) -> np.ndarray:
        return self.score(Z) > self.threshold


class Anomaly:
    def __init__(self, spec: MLSpec, df: pd.DataFrame, fams: dict[str, str], split: Split, seed: int) -> None:
        self.spec, self.split, self.seed = spec, split, seed
        self.task, self.metric = "anomaly", spec.metric
        self.df = df.reset_index(drop=True)
        self.schema = [{"name": f.column, "family": "numeric"} for f in spec.features]
        X, _ = model_matrix(self.df, self.schema)
        self.raw = X.to_numpy(dtype=float)
        self.season = int(spec.season_length or 1)
        lagged = np.vstack([np.full((self.season, self.raw.shape[1]), np.nan), self.raw[:-self.season]]) \
            if len(self.raw) > self.season else np.full_like(self.raw, np.nan)
        self.residual = self.raw - lagged
        self.labels = to_bool(self.df[spec.target])[0].to_numpy() if spec.target else None
        self.has_labels = self.labels is not None and not np.isnan(self.labels).all()
        self.X = X

    def _matrix(self, name: str, rows: np.ndarray) -> np.ndarray:
        Z = self.residual if name == "seasonal_residual" else self.raw
        Z = Z[rows]
        med = np.nanmedian(self.raw[self.split.train], axis=0)
        return np.where(np.isnan(Z), 0.0 if name == "seasonal_residual" else med, Z)

    def _detector(self, name: str, params: dict[str, Any], rows: np.ndarray) -> _Detector:
        return _Detector(name, params, self.seed, self.season).fit(self._matrix(name, rows), self.spec.contamination)

    def _metric(self, metric: str, flags: np.ndarray, rows: np.ndarray) -> float:
        if not self.has_labels:
            return -abs(float(flags.mean()) - self.spec.contamination)  # calibration closeness only
        t = self.labels[rows] == 1
        tp, fp, fn = float((flags & t).sum()), float((flags & ~t).sum()), float((~flags & t).sum())
        if metric == "precision":
            return tp / (tp + fp) if tp + fp else 0.0
        if metric == "recall":
            return tp / (tp + fn) if tp + fn else float("nan")
        return 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else float("nan")

    def _rates(self, flags: np.ndarray, rows: np.ndarray) -> dict[str, Any]:
        out = {"alarm_rate": round(float(flags.mean()), 6) if len(flags) else None}
        if self.has_labels:
            t = self.labels[rows] == 1
            neg = float((~t).sum())
            out.update({m: round(V.finite(self._metric(m, flags, rows)) or 0.0, 6) for m in ("precision", "recall", "f1")})
            out["false_alarm_rate"] = round(float((flags & ~t).sum()) / neg, 6) if neg else None
        return out

    def cv(self, name: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        scores, false_alarms = [], []
        for tr, va in self.split.folds:
            det = self._detector(name, params, tr)
            flags = det.flag(self._matrix(name, va))
            scores.append(V.finite(self._metric(self.metric, flags, va)))
            false_alarms.append(self._rates(flags, va).get("false_alarm_rate", self._rates(flags, va)["alarm_rate"]))
        valid = [s for s in scores if s is not None]
        return {"folds": scores, "mean": float(np.mean(valid)) if valid else None, "backtest_false_alarm_rate": false_alarms}

    def evaluate(self, baseline: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
        tr, ho = self.split.train, self.split.holdout
        dets = {"baseline": self._detector(baseline["estimator"], baseline["params"], tr),
                "candidate": self._detector(best["estimator"], best["params"], tr)}
        self._dets = dets
        flags = {k: d.flag(self._matrix(d.name, ho)) for k, d in dets.items()}
        report: dict[str, Any] = {"holdout_rows": int(len(ho)), "metric": self.metric, "higher_is_better": True,
                                  "labels": bool(self.has_labels)}
        for k, d in dets.items():
            report[k] = {"threshold": round(d.threshold, 6), "threshold_basis":
                         f"{1 - self.spec.contamination:.3f} quantile of training scores", **self._rates(flags[k], ho)}

        def pair(idx: np.ndarray) -> tuple[float, float]:
            return self._metric(self.metric, flags["candidate"][idx], ho[idx]), \
                self._metric(self.metric, flags["baseline"][idx], ho[idx])
        boot = V.paired_bootstrap(self.metric, pair, len(ho), seed=self.seed, confirm_level=V.confirm_level(self.spec))
        report["uncertainty"] = boot
        if self.has_labels:
            report["decision"] = V.decide(self.metric, report["candidate"].get(self.metric),
                                          report["baseline"].get(self.metric), boot, self.spec.min_improvement,
                                          confirmation=self.spec.confirmation,
                                          folds=(best.get("folds"), baseline.get("folds")))
        else:
            report["decision"] = {"improved": False, "gain": None, "reason": "no labels: detection quality cannot be "
                                  "evaluated; thresholds are calibrated and alarm rates reported only"}
        report["guardrails"], report["slices"] = [], []
        report["predictions_hash"] = hashlib.sha256(flags["candidate"].astype(np.int8).tobytes()).hexdigest()
        return report

    def reproduce(self, best: dict[str, Any], expected_hash: str) -> dict[str, Any]:
        det = self._detector(best["estimator"], best["params"], self.split.train)
        got = hashlib.sha256(det.flag(self._matrix(det.name, self.split.holdout)).astype(np.int8).tobytes()).hexdigest()
        return {"check": "reproducible_from_manifest", "outcome": "pass" if got == expected_hash else "fail",
                "reason": "a refit flags the same holdout rows" if got == expected_hash else "a refit flagged other rows"}

    def package(self, best: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        return {"task": "anomaly", "estimator": best["estimator"], "params": best["params"], "schema": self.schema,
                "detector": self._dets["candidate"], "fill": [float(v) for v in np.nanmedian(self.raw[self.split.train], axis=0)],
                "metric": self.metric, "season_length": self.season,
                "reference_profile": reference_profile(self.X.iloc[self.split.train], self.schema)}


def score_rows(package: dict[str, Any], df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Scores each input row with the calibrated detector (seasonal residual needs time-ordered input)."""
    X, reasons = model_matrix(df, package["schema"])
    ok = (reasons == "").to_numpy()
    out = pd.DataFrame(index=df.index, data={"prediction_label": None, "prediction_value": np.nan})
    if ok.any():
        det: _Detector = package["detector"]
        Z = X[ok].to_numpy(dtype=float)
        if det.name == "seasonal_residual":
            m = det.season
            lag = np.vstack([np.full((m, Z.shape[1]), np.nan), Z[:-m]]) if len(Z) > m else np.full_like(Z, np.nan)
            Z = np.where(np.isnan(Z - lag), 0.0, Z - lag)
        else:
            Z = np.where(np.isnan(Z), np.asarray(package["fill"]), Z)
        s = det.score(Z)
        out.loc[ok, "prediction_value"] = s
        out.loc[ok, "prediction_label"] = np.where(s > det.threshold, "anomaly", "normal")
    return out, reasons
