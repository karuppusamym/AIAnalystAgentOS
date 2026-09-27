"""Readiness and leakage checks (P5-01, workspace spec §6): run before any split or fit.

Each check is pass | fail | not_applicable with a reason. Any failure refuses the experiment: a model
trained on leaked information would look better than it can ever be in use, so leakage is never a
warning. Checks: referenced columns exist; the target is usable; declared availability (a feature only
known after the outcome); the target (or an entity key) used as a feature; a feature that alone
determines the target (target-derived); feature timestamps after the prediction cutoff; feature
timestamps at or after the outcome; label maturity (rows whose label horizon has not closed are
excluded); series grain; group-aware splitting for repeated entities; and the row, feature and minimum
size caps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from analystos.contracts.work import MLSpec
from analystos.ml.data import to_datetime, to_number

PURITY_LIMIT = 0.995  # one feature predicting the target this well is treated as derived from it
CORRELATION_LIMIT = 0.995
MIN_ROWS = {"classify": 40, "regress": 40, "cluster": 30, "anomaly": 30, "forecast": 12}
MIN_CLASS_ROWS = 5


@dataclass
class Readiness:
    checks: list[dict[str, Any]] = field(default_factory=list)
    keep: pd.Series | None = None  # rows usable for learning (labels mature and present)
    excluded: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, check: str, outcome: str, reason: str, **details: Any) -> None:
        self.checks.append({"check": check, "outcome": outcome, "reason": reason, **({"details": details} if details else {})})

    @property
    def problems(self) -> list[str]:
        return [f"{c['check']}: {c['reason']}" for c in self.checks if c["outcome"] == "fail"]

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": self.checks, "problems": self.problems, "excluded": dict(self.excluded),
                "usable_rows": int(self.keep.sum()) if self.keep is not None else 0, "warnings": list(self.warnings)}


def referenced_columns(spec: MLSpec) -> list[str]:
    cols = [spec.target, spec.time_column, spec.cutoff_column, spec.outcome_time_column, *spec.entity_keys,
            *spec.group_keys, *[f.column for f in spec.features], *[f.timestamp_column for f in spec.features],
            *[g.column for g in spec.guardrails]]
    return list(dict.fromkeys(c for c in cols if c))


def _cutoff(spec: MLSpec, df: pd.DataFrame) -> pd.Series | None:
    if spec.cutoff_column:
        return to_datetime(df[spec.cutoff_column])
    if spec.prediction_cutoff:
        ts = to_datetime(pd.Series([spec.prediction_cutoff]))[0]
        return pd.Series([ts] * len(df), index=df.index)
    return None


def _timestamps(spec: MLSpec, df: pd.DataFrame, fams: dict[str, str]) -> dict[str, pd.Series]:
    """Per checked feature: when its value was observed (its timestamp column, or itself when a datetime)."""
    out = {}
    for f in spec.features:
        if f.available_at == "known_in_advance":
            continue
        if f.timestamp_column:
            out[f.column] = to_datetime(df[f.timestamp_column])
        elif (f.type or fams.get(f.column)) == "datetime":
            out[f.column] = to_datetime(df[f.column])
    return out


def _binary_stump_accuracy(x: np.ndarray, y: np.ndarray) -> float:
    """Best accuracy of a single threshold on x for a 0/1 target (either direction)."""
    order = np.argsort(x, kind="mergesort")
    xs, ys = x[order], y[order]
    n = len(ys)
    pos_total = ys.sum()
    cum_pos = np.cumsum(ys)
    # split after index i where the value changes
    change = np.nonzero(np.diff(xs) != 0)[0]
    best = max(pos_total, n - pos_total) / n
    for i in change:
        left_pos = cum_pos[i]
        left_n = i + 1
        acc_a = ((left_n - left_pos) + (pos_total - left_pos)) / n  # left negative, right positive
        best = max(best, acc_a, 1 - acc_a)
    return float(best)


def _determines_target(spec: MLSpec, feature: str, fam: str, x: pd.Series, y: pd.Series) -> tuple[float, str] | None:
    """(score, how) when one feature alone reproduces the target, else None."""
    mask = x.notna() & y.notna()
    if mask.sum() < 20:
        return None
    x, y = x[mask], y[mask]
    if spec.task == "regress":
        xn, _ = to_number(x)
        yn, _ = to_number(y)
        ok = xn.notna() & yn.notna()
        if ok.sum() < 20 or xn[ok].nunique() < 3 or yn[ok].nunique() < 3:
            return None
        rho = pd.Series(xn[ok].rank()).corr(pd.Series(yn[ok].rank()))
        return (abs(float(rho)), "rank correlation") if rho is not None and abs(rho) >= CORRELATION_LIMIT else None
    labels = y.astype(str)
    classes = labels.value_counts()
    if len(classes) < 2 or classes.min() / len(labels) < 0.01:
        return None
    if fam in ("numeric", "datetime", "boolean"):
        xn = to_number(x)[0] if fam != "datetime" else to_datetime(x).astype("int64").astype(float)
        ok = xn.notna()
        if len(classes) != 2 or ok.sum() < 20:
            return None
        yb = (labels[ok] == classes.index[0]).to_numpy(dtype=float)
        acc = _binary_stump_accuracy(xn[ok].to_numpy(dtype=float), yb)
        return (acc, "single threshold") if acc >= PURITY_LIMIT else None
    xs = x.astype(str)
    if xs.nunique() > 0.5 * len(xs):
        return None  # near-unique text: an identifier, not a derived label (entity keys are refused separately)
    majority = pd.crosstab(xs, labels).max(axis=1).sum() / len(xs)
    return (float(majority), "category -> class") if majority >= PURITY_LIMIT else None


def check(spec: MLSpec, df: pd.DataFrame, fams: dict[str, str], *, as_of: str | None, max_rows: int,
          max_features: int) -> Readiness:
    r = Readiness()
    n = len(df)
    keep = pd.Series(True, index=df.index)
    problems = spec.executable_problems()
    r.add("spec_executable", "fail" if problems else "pass", "; ".join(problems) or "the spec names everything a run needs")
    missing = [c for c in referenced_columns(spec) if c not in df.columns]
    r.add("columns_exist", "fail" if missing else "pass",
          f"not in the dataset: {', '.join(missing)}" if missing else "every referenced column exists")
    if problems or missing:
        r.keep = keep & False
        return r
    features = [f.column for f in spec.features]
    if n > max_rows:
        r.add("row_cap", "fail", f"{n} rows exceed the cap of {max_rows}; filter the dataset (a sample would change the "
                                 "result silently)")
    else:
        r.add("row_cap", "pass", f"{n} rows <= {max_rows}")
    r.add("feature_cap", "fail" if len(features) > max_features else "pass", f"{len(features)} features (cap {max_features})")

    # ---- declared availability and misuse of the target / keys as features
    late = [f.column for f in spec.features if f.available_at == "after_outcome"]
    r.add("feature_availability", "fail" if late else "pass",
          f"only known after the outcome: {', '.join(late)}" if late else "every feature is available at prediction time")
    leaked = [c for c in features if c == spec.target]
    keys = [c for c in features if c in set(spec.entity_keys) | set(spec.group_keys)]
    r.add("target_not_a_feature", "fail" if leaked else "pass",
          "the target is listed as a feature" if leaked else "the target is not a feature")
    r.add("keys_not_features", "fail" if keys else "pass",
          f"entity/group keys used as features: {', '.join(keys)}" if keys else "no identifier is a feature")

    # ---- target usability and label maturity
    if spec.target and spec.task in ("classify", "regress", "forecast", "anomaly"):
        null_target = df[spec.target].isna()
        if spec.task != "anomaly":
            keep &= ~null_target
            if int(null_target.sum()):
                r.excluded["null_target"] = int(null_target.sum())
    cutoff = _cutoff(spec, df)
    if spec.label_horizon and spec.task in ("classify", "regress"):
        if cutoff is None or as_of is None:
            r.add("label_maturity", "fail", "label_horizon needs cutoff_column or prediction_cutoff to judge maturity")
        else:
            as_of_ts = to_datetime(pd.Series([as_of]))[0]
            mature = (cutoff + pd.Timedelta(days=spec.label_horizon)) <= as_of_ts
            immature = keep & ~mature.fillna(False)
            keep &= mature.fillna(False)
            if int(immature.sum()):
                r.excluded["immature_labels"] = int(immature.sum())
            r.add("label_maturity", "pass", f"{int(immature.sum())} row(s) whose {spec.label_horizon}-day label horizon has "
                                            "not closed are excluded")
    else:
        r.add("label_maturity", "not_applicable", "no label horizon declared")
    if spec.outcome_time_column and cutoff is not None:
        outcome = to_datetime(df[spec.outcome_time_column])
        known = (outcome.notna() & cutoff.notna() & (outcome <= cutoff)) & keep
        if int(known.sum()):
            keep &= ~known
            r.excluded["label_known_at_cutoff"] = int(known.sum())
    if spec.task == "classify" and spec.target:
        counts = df.loc[keep, spec.target].astype(str).value_counts()
        ok = len(counts) >= 2 and int(counts.min()) >= MIN_CLASS_ROWS
        r.add("target_usable", "pass" if ok else "fail",
              f"{len(counts)} classes, smallest {int(counts.min()) if len(counts) else 0} rows" if ok else
              f"classification needs two classes with >= {MIN_CLASS_ROWS} rows each (got {dict(counts.head(5))})")
    elif spec.task in ("regress", "forecast") and spec.target:
        values, bad = to_number(df.loc[keep, spec.target])
        ok = not bool(bad.any()) and values.nunique() >= 3
        r.add("target_usable", "pass" if ok else "fail", "numeric target" if ok else
              f"{spec.task} needs a numeric target with >= 3 distinct values")
    else:
        r.add("target_usable", "not_applicable", "no supervised target")

    # ---- timestamps against the cutoff and the outcome
    stamps = _timestamps(spec, df, fams)
    if stamps and cutoff is not None:
        late_rows = {c: int(((ts > cutoff) & keep).sum()) for c, ts in stamps.items()}
        late_rows = {c: k for c, k in late_rows.items() if k}
        r.add("post_cutoff_timestamps", "fail" if late_rows else "pass",
              "feature values observed after the prediction cutoff: " + ", ".join(f"{c} ({k} rows)" for c, k in late_rows.items())
              if late_rows else "every feature timestamp is at or before the cutoff", rows=late_rows or None)
    elif stamps:
        r.add("post_cutoff_timestamps", "not_applicable", "no prediction cutoff declared (cutoff_column or prediction_cutoff)")
        if spec.task in ("classify", "regress"):
            r.warnings.append("time-stamped features without a prediction cutoff: availability cannot be checked")
    else:
        r.add("post_cutoff_timestamps", "not_applicable", "no time-stamped feature")
    if stamps and spec.outcome_time_column:
        outcome = to_datetime(df[spec.outcome_time_column])
        post = {c: int(((ts >= outcome) & outcome.notna() & keep).sum()) for c, ts in stamps.items()}
        post = {c: k for c, k in post.items() if k}
        r.add("post_outcome_timestamps", "fail" if post else "pass",
              "feature values observed at or after the outcome: " + ", ".join(f"{c} ({k} rows)" for c, k in post.items())
              if post else "no feature is observed at or after the outcome", rows=post or None)
    else:
        r.add("post_outcome_timestamps", "not_applicable", "no outcome time or no time-stamped feature")

    # ---- a feature that alone reproduces the target
    if spec.target and spec.task in ("classify", "regress"):
        derived = {}
        for f in spec.features:
            hit = _determines_target(spec, f.column, f.type or fams.get(f.column, "categorical"), df.loc[keep, f.column],
                                     df.loc[keep, spec.target])
            if hit:
                derived[f.column] = {"score": round(hit[0], 4), "how": hit[1]}
        r.add("target_derived_features", "fail" if derived else "pass",
              "these features alone reproduce the target (derived from it?): " +
              ", ".join(f"{c} ({d['how']} {d['score']})" for c, d in derived.items())
              if derived else "no single feature reproduces the target", features=derived or None)
    else:
        r.add("target_derived_features", "not_applicable", "no supervised target")

    # ---- splitting constraints
    strategy = spec.split.strategy if spec.split else None
    if spec.group_keys and strategy not in (None, "group", "group_chronological"):
        r.add("group_aware_split", "fail", f"group_keys {spec.group_keys} are declared but the split is {strategy}: rows "
                                           "of one group would land in both training and holdout")
    elif spec.entity_keys and strategy == "random" and df.loc[keep, spec.entity_keys].duplicated().any():
        r.add("group_aware_split", "fail", f"entity {spec.entity_keys} repeats across rows; a random split would put one "
                                           "entity in both training and holdout (declare group_keys)")
    else:
        r.add("group_aware_split", "pass", "no entity crosses partitions under this strategy")
    if spec.time_column and (spec.task == "forecast" or strategy in ("chronological", "group_chronological")):
        ts = to_datetime(df[spec.time_column])
        bad = int((ts.isna() & keep).sum())
        r.add("time_column", "fail" if bad else "pass", f"{bad} row(s) have no parseable {spec.time_column}" if bad
              else f"{spec.time_column} parses on every row")
        if spec.task == "forecast":
            dup = int(ts[keep].duplicated().sum())
            r.add("series_grain", "fail" if dup else "pass",
                  f"{dup} repeated {spec.time_column} value(s): a forecast needs one row per period (aggregate first)"
                  if dup else "one row per period")
    usable = int(keep.sum())
    need = MIN_ROWS[spec.task] if spec.task != "forecast" else max(MIN_ROWS["forecast"], 3 * (spec.horizon or 1) + 4)
    r.add("minimum_rows", "pass" if usable >= need else "fail",
          f"{usable} usable rows (need >= {need})" if usable >= need else
          f"insufficient data: {usable} usable rows, need >= {need} (insufficient evidence is a valid outcome)")
    r.keep = keep
    return r
