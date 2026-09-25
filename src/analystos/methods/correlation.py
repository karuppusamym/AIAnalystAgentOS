"""correlation: numeric outcome (y) vs a numeric driver (x = drivers[0], or segment).

primary       Spearman (or Pearson) with a Fisher-z CI on a deterministic pair sample
verification  the other coefficient + a bootstrap CI of the primary one
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy import stats as sps

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.core.errors import InvalidInput
from analystos.methods.base import (
    TABLE_MAX_ROWS,
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    Rows,
    Semantic,
    finish,
    first,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st


def pairs(rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, dict]:
    x = np.array([float(r["x"]) for r in rows])
    y = np.array([float(r["y"]) for r in rows])
    total = int(first(rows, "_rows_total", 0))
    excl = int(first(rows, "_rows_excluded", 0))
    return x, y, {"rows_total": total, "excluded_null_rows": excl, "sample_size": len(rows), "sampled": total - excl > len(rows)}


def labels(spec: AnalysisSpec) -> tuple[str, str]:
    xd = spec.drivers[0] if spec.drivers else spec.segment
    return sb.describe(xd), sb.describe(spec.outcome)


class Correlation(AnalysisMethod):
    name = "correlation"
    vocabulary = "numeric outcome vs numeric drivers[0]."
    outcome_types = frozenset({"numeric"})

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        if spec.outcome is None or not spec.drivers:
            return [f"{self.name} needs a numeric outcome and one numeric driver"]
        return []

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        sb.require(spec, "outcome")
        xd = spec.drivers[0] if spec.drivers else spec.segment
        if xd is None:
            raise InvalidInput(f"{self.name} requires drivers[0] (x) and outcome (y)")
        assert spec.outcome is not None
        cols = [("x", sb.as_double(sb.derive(xd, dialect), dialect)), ("y", sb.as_double(sb.derive(spec.outcome, dialect), dialect))]
        q = sb.sample_query(spec, dialect, cols, sample_rows)
        notes = [f"x = {sb.describe(xd)}, y = {sb.describe(spec.outcome)}; sample of at most {sample_rows} pairs"]
        return sb.CompiledQuery(sb.to_sql(q, dialect), {"x": "x", "y": "y", "rows_total": "_rows_total",
                                                        "rows_excluded": "_rows_excluded"}, notes, purpose, dialect,
                                sample_rows, "sample")

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        x, y, info = pairs(rows["primary"])
        xl, yl = labels(spec)
        stat = st.correlation(x, y, alpha=alpha, x_label=xl, y_label=yl)
        stat.details.update(info)
        out = [[float(a), float(b)] for a, b in zip(x[:TABLE_MAX_ROWS], y[:TABLE_MAX_ROWS], strict=False)]
        return AnalysisOutcome(method=spec.method, stat=stat, table={"columns": [xl, yl], "rows": out})

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        x, y, _ = pairs(rows["primary"])
        prim_method = primary.test if primary.test in ("spearman", "pearson") else "spearman"
        other = "pearson" if prim_method == "spearman" else "spearman"
        xl, yl = labels(spec)
        o = st.correlation(x, y, method=other, alpha=alpha, x_label=xl, y_label=yl)
        if o.statistic is None:
            return finish(spec, primary, o, None, {"columns": [], "rows": []})
        ok = np.isfinite(x) & np.isfinite(y)
        xs, ys = x[ok], y[ok]
        if xs.size > 5000:
            idx = np.random.default_rng(0).choice(xs.size, 5000, replace=False)
            xs, ys = xs[idx], ys[idx]
        if prim_method == "spearman":
            xs, ys = sps.rankdata(xs), sps.rankdata(ys)
        rng = np.random.default_rng(0)
        boots = []
        for _ in range(1000):
            i = rng.integers(0, xs.size, xs.size)
            a, b = xs[i], ys[i]
            if np.ptp(a) == 0 or np.ptp(b) == 0:
                continue
            boots.append(np.corrcoef(a, b)[0, 1])
        lo, hi = (np.quantile(boots, [alpha / 2, 1 - alpha / 2]) if boots else (np.nan, np.nan))
        excl0 = bool(lo > 0 or hi < 0)
        supported = bool(o.supported) and excl0
        direction = (o.statistic > 0) == ((primary.statistic or 0) > 0)
        o.test = f"{other}+bootstrap_{prim_method}"
        o.supported = supported
        o.details["bootstrap_primary_ci"] = [st._f(lo), st._f(hi)]
        o.highlights["bootstrap_primary_ci"] = [st._q(lo), st._q(hi)]
        table = {"columns": ["coefficient", "value", "ci_low", "ci_high"],
                 "rows": [[other, o.statistic, o.ci_low, o.ci_high], [f"bootstrap_{prim_method}", primary.statistic,
                                                                       st._f(lo), st._f(hi)]]}
        return finish(spec, primary, o, direction, table)

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="relationship", dimension="x", measure="outcome")
