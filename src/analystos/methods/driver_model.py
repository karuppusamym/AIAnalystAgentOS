"""driver_model: a boolean outcome vs several drivers.

primary       logistic regression (LR test, odds ratios, holdout AUC) on a deterministic row sample
verification  random-forest grouped permutation importance; agrees when the regression's top driver
              is among the forest's top two
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

import numpy as np

from analystos.contracts.analysis import AnalysisSpec, Derivation, StatResult
from analystos.core.errors import InvalidInput
from analystos.core.ids import stable_hash
from analystos.methods.base import (
    OTHER,
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    Rows,
    Semantic,
    cap,
    finish,
    first,
    is_number,
    no_data,
    seg_label,
    text_parts,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

BOOLEAN_OUT = {"equals", "is_true", "after_hours"}


def build_design(rows: list[dict[str, Any]], drivers: list[Derivation], *, max_levels: int = 10
                 ) -> tuple[np.ndarray, list[str], dict[str, list[int]]]:
    """Driver columns d_0..d_k -> design matrix. Numeric drivers enter as-is; categorical drivers
    are one-hot encoded (up to `max_levels` most frequent levels, the rest pooled as "(other)"),
    dropping the most frequent level as the reference."""
    cols: list[np.ndarray] = []
    names: list[str] = []
    groups: dict[str, list[int]] = {}
    used: set[str] = set()
    for i, d in enumerate(drivers):
        vals = [r[f"d_{i}"] for r in rows]
        name = sb.describe(d)
        while name in used:
            name += "_"
        used.add(name)
        if d.type in sb.BOOLEAN_DERIVATIONS or all(is_number(v) for v in vals):
            groups[name] = [len(names)]
            names.append(name)
            cols.append(np.array([float(v) for v in vals]))
            continue
        labels = [seg_label(v, d) for v in vals]
        freq = Counter(labels).most_common()
        top = [lv for lv, _ in freq[:max_levels]]
        labels = [lv if lv in top else OTHER for lv in labels]
        levels = [lv for lv, _ in Counter(labels).most_common()]
        idx = []
        for lv in levels[1:]:
            idx.append(len(names))
            names.append(f"{name}={lv}")
            cols.append(np.array([1.0 if x == lv else 0.0 for x in labels]))
        groups[name] = idx
    X = np.column_stack(cols) if cols else np.zeros((len(rows), 0))
    return X, names, groups


def driver_data(rows: list[dict[str, Any]], spec: AnalysisSpec) -> tuple[np.ndarray, np.ndarray, list[str], dict[str, list[int]], dict]:
    X, names, groups = build_design(rows, spec.drivers)
    y = np.array([float(r["outcome"]) for r in rows])
    total = int(first(rows, "_rows_total", 0))
    excl = int(first(rows, "_rows_excluded", 0))
    return X, y, names, groups, {"rows_total": total, "excluded_null_rows": excl, "sample_size": len(rows),
                                 "sampled": total - excl > len(rows)}


class DriverModel(AnalysisMethod):
    name = "driver_model"
    vocabulary = "boolean outcome vs 2-5 drivers (logistic regression + feature importance)."
    outcome_types = frozenset({"boolean"})

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        if spec.outcome is None or spec.outcome.type not in BOOLEAN_OUT or len(spec.drivers) < 2:
            return [f"{self.name} needs a boolean outcome and at least two drivers"]
        return []

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        sb.require(spec, "outcome")
        if not spec.drivers:
            raise InvalidInput(f"{self.name} requires at least one driver")
        assert spec.outcome is not None
        o = spec.outcome
        out_e = sb.derive(o, dialect) if o.type in sb.BOOLEAN_DERIVATIONS else sb.is_true_expr(sb.derive(o, dialect), dialect)
        cols = []
        columns = {}
        for i, d in enumerate(spec.drivers):
            cols.append((f"d_{i}", sb.derive(d, dialect)))
            columns[f"driver_{i}"] = f"d_{i}"
        cols.append(("outcome", out_e))
        columns.update({"outcome": "outcome", "rows_total": "_rows_total", "rows_excluded": "_rows_excluded"})
        q = sb.sample_query(spec, dialect, cols, sample_rows)
        notes = [f"drivers: {[sb.describe(d) for d in spec.drivers]}; sample of at most {sample_rows} rows"]
        return sb.CompiledQuery(sb.to_sql(q, dialect), columns, notes, purpose, dialect, sample_rows, "sample")

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        X, y, names, groups, info = driver_data(rows["primary"], spec)
        if X.shape[0] == 0:
            return no_data(spec, "no complete rows", "logistic_regression")
        stat = st.logistic_regression(X, y, names, feature_groups=groups, alpha=alpha)
        stat.details.update(info | {"feature_groups": groups, "outcome": sb.describe(spec.outcome)})
        stat.highlights["outcome"] = sb.describe(spec.outcome)
        cols = ["feature", "odds_ratio", "ci_low", "ci_high", "p_value"]
        table = {"columns": cols, "rows": [[g.get(c) for c in cols] for g in stat.groups]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        X, y, names, groups, _ = driver_data(rows["primary"], spec)
        fi = st.feature_importance(X, y, names, feature_groups=groups, seed=0)
        top_lr = primary.highlights.get("top_driver")
        ranking = fi.highlights.get("ranking") or []
        direction = top_lr in ranking[:2] if top_lr else None
        fi.details["primary_top_driver"] = top_lr
        table = {"columns": ["driver", "importance_mean", "importance_std", "rank"],
                 "rows": [[g["driver"], g["importance_mean"], g["importance_std"], g["rank"]] for g in fi.groups]}
        return finish(spec, primary, fi, direction, table)

    def claim_subject(self, spec: Mapping[str, Any]) -> Any:
        return "drivers:" + ",".join(sorted(d.get("column", "") for d in spec.get("drivers") or []))

    def identity_keys(self, spec: AnalysisSpec) -> list[Any]:
        """A second driver model on the same outcome and population re-answers the same question with a
        different feature list (seen live: three identical conclusions)."""
        if spec.outcome is None:
            return []
        filters = sorted(f"{f.column}{f.op}{f.value}" for f in spec.filters or [])
        return [stable_hash({"m": self.name, "o": spec.outcome.model_dump(), "f": filters})]

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        if not hl.get("top_driver"):
            return None
        _, out, scope = text_parts(spec)
        title = f"{cap(out)} is driven mainly by {hl['top_driver']}"
        feature, odds = hl.get("strongest_feature"), hl.get("strongest_odds_ratio")
        text = (f"Among the drivers tested, {hl['top_driver']} explains the most of {out}"
                + (f"; {feature} has {odds:.1f}x the odds" if feature and isinstance(odds, (int, float)) else "")
                + (f" (holdout AUC {hl['holdout_roc_auc']:.2f})" if isinstance(hl.get("holdout_roc_auc"), (int, float)) else "")
                + f"{scope}.")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="comparison", dimension="drivers", measure="outcome")
