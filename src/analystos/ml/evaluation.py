"""Metrics, paired uncertainty, slices and calibration (P5-02). Pure numpy/scikit-learn.

Every comparison is *paired*: baseline and candidate are scored on the same rows, and the bootstrap
resamples those rows once for both, so the interval of the gain reflects only the models' difference.
"Gain" is always oriented so that positive means the candidate is better.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np

from analystos.contracts.work import LOWER_IS_BETTER

BOOTSTRAP = 200
CI_LEVEL = 0.95
CONFIRM_BOOTSTRAP = 1000


def higher_is_better(metric: str) -> bool:
    return metric not in LOWER_IS_BETTER


def gain(metric: str, candidate: float, baseline: float) -> float:
    return (candidate - baseline) if higher_is_better(metric) else (baseline - candidate)


def finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ------------------------------------------------------------------------------------ classification
def positive_class(classes: list[str], y: np.ndarray, declared: Any) -> str:
    if declared is not None and str(declared) in classes:
        return str(declared)
    for pos in ("1", "True", "true", "yes", "Y", "1.0"):
        if pos in classes and len(classes) == 2:
            return pos
    counts = {c: int((y == c).sum()) for c in classes}
    return min(classes, key=lambda c: (counts[c], c))  # the minority class


def classify_metric(metric: str, y: np.ndarray, proba: np.ndarray, classes: list[str], positive: str,
                    threshold: float | None = None) -> float:
    from sklearn import metrics as skm

    if len(np.unique(y)) < 2 and metric in ("roc_auc",):
        return float("nan")
    binary = len(classes) == 2
    if metric == "roc_auc":
        if binary:
            return binary_auc(y == positive, proba[:, classes.index(positive)])
        return float(skm.roc_auc_score(y, proba, multi_class="ovr", average="weighted", labels=classes))
    if metric == "log_loss":
        return float(skm.log_loss(y, np.clip(proba, 1e-15, 1 - 1e-15), labels=classes))
    labels = predict_labels(proba, classes, positive, threshold)
    if metric == "accuracy":
        return float(skm.accuracy_score(y, labels))
    if metric == "balanced_accuracy":
        return float(skm.balanced_accuracy_score(y, labels)) if len(np.unique(y)) > 1 else float("nan")
    if metric == "f1":
        if binary:
            return float(skm.f1_score(y == positive, labels == positive, zero_division=0))
        return float(skm.f1_score(y, labels, average="macro", labels=classes, zero_division=0))
    if metric == "precision":
        return float(skm.precision_score(y == positive, labels == positive, zero_division=0))
    if metric == "recall":
        return float(skm.recall_score(y == positive, labels == positive, zero_division=0))
    raise ValueError(f"unknown classification metric {metric}")


def binary_auc(truth: np.ndarray, score: np.ndarray) -> float:
    """ROC AUC as the Mann-Whitney statistic (ties at half credit): the value `roc_auc_score` gives, without its
    per-call validation overhead, which dominated the paired bootstrap (P5-07 reads 1000 resamples)."""
    from scipy.stats import rankdata

    t = np.asarray(truth, dtype=bool)
    n1 = int(t.sum())
    n0 = len(t) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = rankdata(np.asarray(score, dtype=float))
    return float((ranks[t].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def predict_labels(proba: np.ndarray, classes: list[str], positive: str, threshold: float | None) -> np.ndarray:
    if len(classes) == 2 and threshold is not None:
        neg = next(c for c in classes if c != positive)
        return np.where(proba[:, classes.index(positive)] >= threshold, positive, neg)
    return np.asarray(classes, dtype=object)[np.argmax(proba, axis=1)]


def choose_threshold(y: np.ndarray, p_pos: np.ndarray, positive: str, fp_cost: float, fn_cost: float) -> dict[str, Any]:
    """The probability threshold minimizing expected error cost on out-of-fold validation predictions
    (never on the holdout)."""
    t = y == positive
    candidates = np.unique(np.round(np.quantile(p_pos, np.linspace(0.01, 0.99, 99)), 6)) if len(p_pos) else np.array([0.5])
    best = (math.inf, 0.5)
    for c in np.concatenate([candidates, [0.5]]):
        pred = p_pos >= c
        cost = fp_cost * float((pred & ~t).sum()) + fn_cost * float((~pred & t).sum())
        if cost < best[0] - 1e-12 or (abs(cost - best[0]) <= 1e-12 and abs(c - 0.5) < abs(best[1] - 0.5)):
            best = (cost, float(c))
    return {"threshold": best[1], "expected_cost_per_row": best[0] / max(len(t), 1), "costs": {"false_positive": fp_cost,
            "false_negative": fn_cost}, "chosen_on": "out-of-fold validation predictions"}


def confusion(y: np.ndarray, labels: np.ndarray, positive: str) -> dict[str, int]:
    t, p = y == positive, labels == positive
    return {"tp": int((t & p).sum()), "fp": int((~t & p).sum()), "fn": int((t & ~p).sum()), "tn": int((~t & ~p).sum())}


def calibration(y: np.ndarray, p_pos: np.ndarray, positive: str, bins: int = 10) -> dict[str, Any]:
    t = (y == positive).astype(float)
    edges = np.linspace(0, 1, bins + 1)
    out, ece = [], 0.0
    for i in range(bins):
        m = (p_pos >= edges[i]) & ((p_pos < edges[i + 1]) if i < bins - 1 else (p_pos <= 1))
        if not m.any():
            continue
        pred, obs = float(p_pos[m].mean()), float(t[m].mean())
        ece += m.sum() / len(t) * abs(pred - obs)
        out.append({"bin": [round(edges[i], 2), round(edges[i + 1], 2)], "rows": int(m.sum()), "mean_predicted": round(pred, 4),
                    "observed_rate": round(obs, 4)})
    return {"bins": out, "expected_calibration_error": round(ece, 4),
            "brier": round(float(np.mean((p_pos - t) ** 2)), 6) if len(t) else None}


# ------------------------------------------------------------------------------------ regression
def regress_metric(metric: str, y: np.ndarray, pred: np.ndarray) -> float:
    err = pred - y
    if metric == "mae":
        return float(np.mean(np.abs(err)))
    if metric == "rmse":
        return float(np.sqrt(np.mean(err ** 2)))
    if metric == "r2":
        ss = float(np.sum((y - y.mean()) ** 2))
        return float(1 - np.sum(err ** 2) / ss) if ss > 0 else float("nan")
    raise ValueError(f"unknown regression metric {metric}")


def residuals(y: np.ndarray, pred: np.ndarray, unit: str | None) -> dict[str, Any]:
    r = y - pred
    abs_r = np.abs(r)
    corr = float(np.corrcoef(abs_r, pred)[0, 1]) if len(r) > 2 and np.std(pred) > 0 and np.std(abs_r) > 0 else None
    return {"unit": unit, "mean_residual": round(float(r.mean()), 6), "residual_std": round(float(r.std()), 6),
            "abs_error_quantiles": {q: round(float(np.quantile(abs_r, q / 100)), 6) for q in (50, 90, 99)},
            "abs_error_vs_prediction_corr": round(corr, 4) if corr is not None else None,
            "note": "errors in the target's business unit" + (f" ({unit})" if unit else "")}


# ------------------------------------------------------------------------------------ paired bootstrap
def paired_bootstrap(metric: str, fn: Callable[[np.ndarray], tuple[float, float]], n: int, *, seed: int,
                     b: int = BOOTSTRAP, confirm_level: float | None = None) -> dict[str, Any]:
    """`fn(indices) -> (candidate value, baseline value)`. The CI of the gain, of each side, and the share of
    resamples in which the candidate was better. With `confirm_level` above `CI_LEVEL` (P5-07) the lower bound
    of the gain at that level is added as `confirm_low`, from at least `CONFIRM_BOOTSTRAP` resamples so that a
    far-tail quantile is not read off the one or two smallest values."""
    confirming = confirm_level is not None and confirm_level > CI_LEVEL
    if confirming:
        b = max(b, CONFIRM_BOOTSTRAP)
    rng = np.random.default_rng(seed)
    gains, cands, bases = [], [], []
    for _ in range(b):
        idx = rng.integers(0, n, n)
        c, bl = fn(idx)
        if finite(c) is None or finite(bl) is None:
            continue
        cands.append(c)
        bases.append(bl)
        gains.append(gain(metric, c, bl))
    lo, hi = (1 - CI_LEVEL) / 2, 1 - (1 - CI_LEVEL) / 2
    if len(gains) < b // 2:
        out = {"resamples": len(gains), "ci_low": None, "ci_high": None, "p_better": None, "level": CI_LEVEL}
        return {**out, "confirm_level": confirm_level, "confirm_low": None} if confirming else out
    out = {"resamples": len(gains), "level": CI_LEVEL,
           "ci_low": round(float(np.quantile(gains, lo)), 6), "ci_high": round(float(np.quantile(gains, hi)), 6),
           "candidate_ci": [round(float(np.quantile(cands, lo)), 6), round(float(np.quantile(cands, hi)), 6)],
           "baseline_ci": [round(float(np.quantile(bases, lo)), 6), round(float(np.quantile(bases, hi)), 6)],
           "p_better": round(float(np.mean(np.asarray(gains) > 0)), 4)}
    if confirming:
        out.update(confirm_level=confirm_level, confirm_low=round(float(np.quantile(gains, (1 - confirm_level) / 2)), 6))
    return out


def confirm_level(spec: Any) -> float | None:
    """The holdout level the spec's confirmation asks for (None: the 95% interval alone decides)."""
    conf = getattr(spec, "confirmation", None)
    return float(conf.holdout_level) if conf is not None and conf.holdout_level > CI_LEVEL else None


def cv_confirmation(metric: str, candidate_folds: list[Any] | None, baseline_folds: list[Any] | None) -> dict[str, Any]:
    """The search's out-of-fold evidence, paired per fold (the same validation rows for both): the candidate
    must be better in a majority of the folds and on average. No holdout row is ever in a fold."""
    pairs = [(finite(c), finite(b)) for c, b in zip(candidate_folds or [], baseline_folds or [], strict=False)]
    gains = [gain(metric, c, b) for c, b in pairs if c is not None and b is not None]
    if not gains:
        return {"folds": 0, "positive": 0, "mean_gain": None, "passed": False,
                "reason": "no paired cross-validation folds to confirm on"}
    positive = sum(g > 0 for g in gains)
    mean = float(np.mean(gains))
    return {"folds": len(gains), "positive": positive, "mean_gain": round(mean, 6),
            "fold_gains": [round(g, 6) for g in gains], "passed": positive * 2 > len(gains) and mean > 0,
            "reason": f"better in {positive} of {len(gains)} cross-validation folds, mean fold gain {mean:.4g}"}


def decide(metric: str, candidate: float | None, baseline: float | None, boot: dict[str, Any], min_improvement: float,
           *, confirmation: Any = None, folds: tuple[list[Any] | None, list[Any] | None] | None = None) -> dict[str, Any]:
    """Improvement = the point gain exceeds `min_improvement` *and* the gain's 95% interval excludes zero *and*
    (P5-07; `confirmation` is the spec's ImprovementConfirmation) the win is confirmed: by the search's paired
    out-of-fold cross-validation (`folds` = (candidate folds, baseline folds)) and by the holdout interval at
    the stricter confirmation level. Anything else is "no improvement": a valid, reportable result (the model
    is not promotable)."""
    if candidate is None or baseline is None:
        return {"improved": False, "gain": None, "reason": "a holdout metric could not be computed"}
    g = gain(metric, candidate, baseline)
    lo = boot.get("ci_low")
    improved = g > min_improvement and lo is not None and lo > 0
    confirmed = _confirm(metric, boot, confirmation, folds) if confirmation is not None else None
    if improved and confirmed is not None and not confirmed["passed"]:
        return {"improved": False, "gain": round(g, 6), "min_improvement": min_improvement, "confirmation": confirmed,
                "reason": f"no improvement: the holdout gain {g:.4g} {metric} is not confirmed ({confirmed['reason']})"}
    if improved:
        reason = f"candidate beats the baseline by {g:.4g} {metric} (95% interval of the gain {lo:.4g}..{boot['ci_high']:.4g})"
        if confirmed is not None:
            reason += f"; confirmed: {confirmed['reason']}"
    elif g <= min_improvement:
        reason = f"no improvement: gain {g:.4g} {metric} is not above the declared minimum {min_improvement:g}"
    else:
        reason = (f"no improvement: gain {g:.4g} {metric} but its 95% interval reaches {lo if lo is not None else 'n/a'}; "
                  "the difference is within noise")
    out = {"improved": bool(improved), "gain": round(g, 6), "min_improvement": min_improvement, "reason": reason}
    return {**out, "confirmation": confirmed} if confirmed is not None else out


def _confirm(metric: str, boot: dict[str, Any], confirmation: Any, folds: Any) -> dict[str, Any]:
    level = float(confirmation.holdout_level)
    parts: dict[str, dict[str, Any]] = {}
    reasons = []
    if confirmation.cross_validation:
        parts["cross_validation"] = cv_confirmation(metric, *(folds or (None, None)))
        reasons.append(parts["cross_validation"]["reason"])
    if level > CI_LEVEL:
        low = boot.get("confirm_low")
        parts["holdout"] = {"level": level, "ci_low": low, "passed": low is not None and low > 0}
        reasons.append(f"the {level:.1%} interval of the holdout gain starts at {low if low is not None else 'n/a'}")
    return {"passed": all(p["passed"] for p in parts.values()), **parts,
            "reason": "; ".join(reasons) or "no confirmation required by the spec"}


# ------------------------------------------------------------------------------------ slices
def slice_values(values: list[Any], numeric: bool) -> tuple[np.ndarray, list[str]]:
    """Labels per row: categories (top 10, rest `(other)`) or quartile bins of a numeric column."""
    arr = np.asarray(values, dtype=object)
    if numeric:
        nums = np.array([finite(v) if finite(v) is not None else np.nan for v in arr], dtype=float)
        ok = ~np.isnan(nums)
        if ok.sum() < 8 or len(np.unique(nums[ok])) < 4:
            labels = np.array(["(null)" if not o else str(v) for v, o in zip(nums, ok, strict=True)], dtype=object)
        else:
            qs = np.quantile(nums[ok], [0.25, 0.5, 0.75])
            names = [f"<= {qs[0]:.4g}", f"({qs[0]:.4g}, {qs[1]:.4g}]", f"({qs[1]:.4g}, {qs[2]:.4g}]", f"> {qs[2]:.4g}"]
            labels = np.array(["(null)" if not o else names[int(np.searchsorted(qs, v, side="left"))]
                               for v, o in zip(nums, ok, strict=True)], dtype=object)
    else:
        labels = np.array(["(null)" if v is None else str(v) for v in arr], dtype=object)
        uniq, counts = np.unique(labels, return_counts=True)
        top = set(uniq[np.argsort(-counts, kind="stable")[:10]])
        labels = np.array([v if v in top else "(other)" for v in labels], dtype=object)
    return labels, sorted(set(labels.tolist()))


def slice_table(metric: str, labels: np.ndarray, fn: Callable[[np.ndarray], tuple[float, float]], *, min_rows: int,
                max_degradation: float | None = None) -> list[dict[str, Any]]:
    out = []
    for value in sorted(set(labels.tolist())):
        idx = np.nonzero(labels == value)[0]
        row: dict[str, Any] = {"slice": value, "rows": int(len(idx))}
        if len(idx) < min_rows:
            row.update(status="not_applicable", reason=f"fewer than {min_rows} holdout rows (untested slice)")
            out.append(row)
            continue
        c, b = fn(idx)
        c, b = finite(c), finite(b)
        row.update(candidate=round(c, 6) if c is not None else None, baseline=round(b, 6) if b is not None else None)
        if c is None or b is None:
            row.update(status="not_applicable", reason="metric undefined in this slice (e.g. one class only)")
        else:
            g = gain(metric, c, b)
            row["gain"] = round(g, 6)
            if max_degradation is not None:
                row["status"] = "fail" if -g > max_degradation + 1e-12 else "pass"
                if row["status"] == "fail":
                    row["reason"] = f"candidate worse than the baseline by {-g:.4g} {metric} (allowed {max_degradation:g})"
            else:
                row["status"] = "reported"
        out.append(row)
    return out
