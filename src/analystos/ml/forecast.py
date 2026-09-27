"""`ml.forecast`: statsmodels ETS / ARIMA against a seasonal-naive baseline, rolling-origin backtests.

The series is one row per period (readiness refuses repeated periods). Validation folds are rolling
origins inside the training part, each `horizon` periods long; the holdout is the last `horizon` periods,
read once after the selection froze. Evidence: error by horizon step (backtest and holdout) and the
coverage of the 80% and 95% prediction intervals.
"""
from __future__ import annotations

import hashlib
import math
import warnings
from typing import Any

import numpy as np
import pandas as pd

from analystos.contracts.work import MLSpec
from analystos.ml import evaluation as V
from analystos.ml.data import to_datetime
from analystos.ml.splits import Split

Z80, Z95 = 1.2815515655446004, 1.959963984540054


def _naive(y: np.ndarray, h: int, m: int) -> dict[str, np.ndarray]:
    m = m if len(y) > m else 1
    point = np.array([y[len(y) - m + (k % m)] for k in range(h)], dtype=float)
    resid = y[m:] - y[:-m] if len(y) > m else np.array([0.0])
    sigma = float(np.std(resid, ddof=1)) if len(resid) > 1 else 0.0
    steps = np.array([math.floor(k / m) + 1 for k in range(h)], dtype=float)
    half80, half95 = Z80 * sigma * np.sqrt(steps), Z95 * sigma * np.sqrt(steps)
    return {"point": point, "lo80": point - half80, "hi80": point + half80, "lo95": point - half95, "hi95": point + half95}


def fit_forecast(name: str, params: dict[str, Any], y: np.ndarray, h: int, m: int | None) -> dict[str, np.ndarray]:
    if name == "seasonal_naive":
        return _naive(y, h, m or 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if name == "ets":
            from statsmodels.tsa.exponential_smoothing.ets import ETSModel

            seasonal = "add" if m and len(y) >= 2 * m + 2 else None
            model = ETSModel(pd.Series(y.astype(float)), error="add", trend=params.get("trend"),
                             damped_trend=bool(params.get("damped")) if params.get("trend") else False,
                             seasonal=seasonal, seasonal_periods=m if seasonal else None)
            res = model.fit(disp=False, maxiter=200)
            pred = res.get_prediction(start=len(y), end=len(y) + h - 1)
            f80, f95 = pred.summary_frame(alpha=0.2), pred.summary_frame(alpha=0.05)
            return {"point": f95["mean"].to_numpy(dtype=float), "lo80": f80["pi_lower"].to_numpy(dtype=float),
                    "hi80": f80["pi_upper"].to_numpy(dtype=float), "lo95": f95["pi_lower"].to_numpy(dtype=float),
                    "hi95": f95["pi_upper"].to_numpy(dtype=float)}
        if name == "arima":
            from statsmodels.tsa.arima.model import ARIMA

            res = ARIMA(y.astype(float), order=tuple(params["order"])).fit()
            fc = res.get_forecast(h)
            c80, c95 = np.asarray(fc.conf_int(alpha=0.2)), np.asarray(fc.conf_int(alpha=0.05))
            return {"point": np.asarray(fc.predicted_mean, dtype=float), "lo80": c80[:, 0], "hi80": c80[:, 1],
                    "lo95": c95[:, 0], "hi95": c95[:, 1]}
    raise ValueError(f"{name} is not an allowlisted forecast estimator")


def _metric(metric: str, err: np.ndarray) -> float:
    return float(np.sqrt(np.mean(err ** 2))) if metric == "rmse" else float(np.mean(np.abs(err)))


class Forecast:
    def __init__(self, spec: MLSpec, df: pd.DataFrame, fams: dict[str, str], split: Split, seed: int) -> None:
        self.spec, self.split, self.seed = spec, split, seed
        self.task, self.metric = "forecast", spec.metric
        self.df = df.reset_index(drop=True)
        self.times = to_datetime(self.df[spec.time_column])
        self.y = pd.to_numeric(self.df[spec.target], errors="coerce").to_numpy(dtype=float)
        self.m = spec.season_length
        self.h = int(spec.horizon or 1)
        self._cv: dict[str, dict[str, Any]] = {}

    def cv(self, name: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        scores, by_h, errors = [], [], []
        for tr, va in self.split.folds:
            f = fit_forecast(name, params, self.y[tr], len(va), self.m)
            err = f["point"] - self.y[va]
            scores.append(V.finite(_metric(self.metric, err)))
            by_h.append(np.abs(err))
            errors.append(err)
        valid = [s for s in scores if s is not None]
        out = {"folds": scores, "mean": float(np.mean(valid)) if valid else None,
               "abs_error_by_horizon": _by_horizon(by_h)}
        self._cv[f"{name}:{params}"] = {"errors": np.concatenate(errors) if errors else np.array([]), **out}
        return out

    def _backtest(self, trial: dict[str, Any]) -> dict[str, Any]:
        key = f"{trial['estimator']}:{trial['params']}"
        if key not in self._cv:
            self.cv(trial["estimator"], trial["params"])
        return self._cv[key]

    def evaluate(self, baseline: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
        tr, ho = self.split.train, self.split.holdout
        fits = {"baseline": fit_forecast(baseline["estimator"], baseline["params"], self.y[tr], len(ho), self.m),
                "candidate": fit_forecast(best["estimator"], best["params"], self.y[tr], len(ho), self.m)}
        actual = self.y[ho]
        report: dict[str, Any] = {"holdout_rows": int(len(ho)), "metric": self.metric, "higher_is_better": False,
                                  "horizon": self.h}
        for side, f in fits.items():
            err = f["point"] - actual
            report[side] = {"mae": round(_metric("mae", err), 6), "rmse": round(_metric("rmse", err), 6),
                            "abs_error_by_horizon": [round(float(abs(e)), 6) for e in err],
                            "coverage_80": round(float(np.mean((actual >= f["lo80"]) & (actual <= f["hi80"]))), 4),
                            "coverage_95": round(float(np.mean((actual >= f["lo95"]) & (actual <= f["hi95"]))), 4),
                            "backtest_abs_error_by_horizon": self._backtest(baseline if side == "baseline" else best)[
                                "abs_error_by_horizon"]}
        # Paired over every out-of-sample error (rolling-origin backtest points and the holdout).
        e_c = np.concatenate([self._backtest(best)["errors"], fits["candidate"]["point"] - actual])
        e_b = np.concatenate([self._backtest(baseline)["errors"], fits["baseline"]["point"] - actual])
        n = min(len(e_c), len(e_b))
        e_c, e_b = e_c[-n:], e_b[-n:]

        def pair(idx: np.ndarray) -> tuple[float, float]:
            return _metric(self.metric, e_c[idx]), _metric(self.metric, e_b[idx])
        boot = V.paired_bootstrap(self.metric, pair, n, seed=self.seed, confirm_level=V.confirm_level(self.spec))
        report["uncertainty"] = {**boot, "pooled_points": n, "note": "paired over backtest and holdout errors"}
        report["decision"] = V.decide(self.metric, report["candidate"][self.metric], report["baseline"][self.metric], boot,
                                      self.spec.min_improvement, confirmation=self.spec.confirmation,
                                      folds=(self._backtest(best)["folds"], self._backtest(baseline)["folds"]))
        report["guardrails"], report["slices"] = [], []
        report["predictions_hash"] = hashlib.sha256(np.round(fits["candidate"]["point"], 8).tobytes()).hexdigest()
        self._fits = fits
        return report

    def reproduce(self, best: dict[str, Any], expected_hash: str) -> dict[str, Any]:
        f = fit_forecast(best["estimator"], best["params"], self.y[self.split.train], len(self.split.holdout), self.m)
        got = hashlib.sha256(np.round(f["point"], 8).tobytes()).hexdigest()
        return {"check": "reproducible_from_manifest", "outcome": "pass" if got == expected_hash else "fail",
                "reason": "a refit on the manifest's training periods gives the same forecast" if got == expected_hash
                else "a refit gave a different forecast"}

    def package(self, best: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        """The frozen specification refit on every period (train + holdout) for the forward forecast."""
        f = fit_forecast(best["estimator"], best["params"], self.y, self.h, self.m)
        ts = self.times.sort_values()
        step = ts.diff().median() if len(ts) > 1 else pd.Timedelta(days=1)
        freq = pd.infer_freq(pd.DatetimeIndex(ts.dt.tz_convert(None))) if len(ts) >= 3 else None
        if freq:
            future = pd.date_range(ts.iloc[-1].tz_convert(None), periods=self.h + 1, freq=freq)[1:]
        else:
            future = [ts.iloc[-1].tz_convert(None) + step * (k + 1) for k in range(self.h)]
        rows = [{"period": pd.Timestamp(p).isoformat(), "step": k + 1, "point": round(float(f["point"][k]), 6),
                 "lo80": round(float(f["lo80"][k]), 6), "hi80": round(float(f["hi80"][k]), 6),
                 "lo95": round(float(f["lo95"][k]), 6), "hi95": round(float(f["hi95"][k]), 6)} for k, p in enumerate(future)]
        return {"task": "forecast", "estimator": best["estimator"], "params": best["params"], "schema": [],
                "target": self.spec.target, "time_column": self.spec.time_column, "season_length": self.m,
                "horizon": self.h, "metric": self.metric, "forecast": rows, "last_period": ts.iloc[-1].isoformat(),
                "reference_profile": {}}


def _by_horizon(errs: list[np.ndarray]) -> list[float]:
    if not errs:
        return []
    h = max(len(e) for e in errs)
    return [round(float(np.mean([e[k] for e in errs if len(e) > k])), 6) for k in range(h)]
