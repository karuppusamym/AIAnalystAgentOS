"""cohort_retention: retention of entity cohorts over time buckets.

An entity (the spec's `outcome` column: a customer, account or device id) joins the cohort of the
first `time` period it is active in. Retention at horizon k is the share of a cohort still active k
periods later. The query is pushed down as one aggregate (cohort x period -> active entities); no
entity id ever leaves the source.

primary       chi-square test that next-period retention differs across cohorts (+ Cramér's V); the
              most recent cohort is excluded because its next period is not observed yet
verification  logistic regression with cohort dummies fitted as a binomial GLM on the same table,
              plus a Monte Carlo permutation test with fixed margins; agrees when the odds ratio of
              the best vs the worst cohort points the same way
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from typing import Any

from sqlglot import exp

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.core.errors import InvalidInput
from analystos.methods.base import (
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    Rows,
    Semantic,
    finish,
    fmt_pct,
    json_value,
    nothing_to_verify,
    select_segments,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st

HORIZON = 1  # retention quoted and tested at the next period
CURVE = 3  # offsets shown in the retention table
MAX_CELLS = 5000


def _day(v: Any) -> _dt.date:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    return _dt.date.fromisoformat(str(v)[:10])


def offset(start: Any, end: Any, grain: str) -> int:
    """Whole `grain` periods from `start` to `end` (both already truncated to the grain)."""
    a, b = _day(start), _day(end)
    months = (b.year - a.year) * 12 + b.month - a.month
    if grain == "month":
        return months
    if grain == "quarter":
        return months // 3
    if grain == "week":
        return (b - a).days // 7
    return (b - a).days


def _col(alias: str, tbl: str) -> exp.Column:
    return exp.column(alias, table=tbl, quoted=True)


def cohorts(rows: list[dict[str, Any]], spec: AnalysisSpec) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Per cohort: size, active entities per offset, and whether the horizon is observed."""
    grain = (spec.time.grain if spec.time else None) or "month"
    last = max((r["period"] for r in rows if r["period"] is not None), default=None)
    by: dict[Any, dict[str, Any]] = {}
    for r in rows:
        if r["cohort"] is None or r["period"] is None:
            continue
        c = by.setdefault(r["cohort"], {"segment": _day(r["cohort"]).isoformat(), "active": {}})
        k = offset(r["cohort"], r["period"], grain)
        c["active"][k] = c["active"].get(k, 0) + int(r["active"] or 0)
    out = []
    for key in sorted(by):
        c = by[key]
        size = c["active"].get(0, 0)
        observed = last is not None and offset(key, last, grain) >= HORIZON
        out.append({"segment": c["segment"], "n": size, "positives": c["active"].get(HORIZON, 0), "observed": observed,
                    "curve": {k: (c["active"].get(k, 0) / size if size else None) for k in range(CURVE + 1)
                              if last is not None and offset(key, last, grain) >= k}})
    return out, {"grain": grain, "cohorts_total": len(out), "censored_cohorts": sum(not c["observed"] for c in out),
                 "truncated": len(rows) >= MAX_CELLS}


class CohortRetention(AnalysisMethod):
    name = "cohort_retention"
    vocabulary = ("retention of entity cohorts: outcome = the entity id column (type column), time = date_trunc of "
                  "its activity timestamp (grain week|month); cohorts are the entity's first active period.")
    outcome_types = frozenset({"identifier", "categorical"})

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        errors = []
        if spec.outcome is None or spec.outcome.type != "column":
            errors.append(f"{self.name} needs outcome = the entity id column (type column)")
        elif semantic(spec.outcome) not in ("id", "categorical", "text", None):
            errors.append(f"outcome {spec.outcome.column} is not an entity identifier")
        if spec.time is None or spec.time.type != "date_trunc" or not spec.time.grain:
            errors.append(f"{self.name} needs time = date_trunc with a grain")
        if spec.segment is not None or spec.drivers:
            errors.append(f"{self.name} takes no segment or drivers (the cohorts are the segments)")
        return errors

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        if spec.outcome is None or spec.time is None:
            raise InvalidInput(f"{self.name} requires `outcome` (entity) and `time`")
        inner = sb.base_select(spec, dialect, [("entity", sb.derive(spec.outcome, dialect)),
                                               ("period", sb.time_period_expr(spec.time, dialect))])
        activity = (exp.select(_col("entity", "d").as_(sb.ident("entity")), _col("period", "d").as_(sb.ident("period")))
                    .distinct().from_(sb.subquery(inner, "d"))
                    .where(sb.and_all([sb.not_null(_col("entity", "d")), sb.not_null(_col("period", "d"))])))
        first = (exp.select(_col("entity", "f").as_(sb.ident("entity")), exp.Min(this=_col("period", "f")).as_(sb.ident("cohort")))
                 .from_(sb.subquery(activity.copy(), "f")).group_by(_col("entity", "f")))
        q = (exp.select(_col("cohort", "c").as_(sb.ident("cohort")), _col("period", "a").as_(sb.ident("period")),
                        sb.count_star().as_(sb.ident("active")))
             .from_(sb.subquery(activity, "a"))
             .join(sb.subquery(first, "c"), on=exp.EQ(this=_col("entity", "a"), expression=_col("entity", "c")))
             .group_by(_col("cohort", "c"), _col("period", "a"))
             .order_by(_col("cohort", "c"), _col("period", "a")).limit(MAX_CELLS))
        notes = [f"entity = {sb.describe(spec.outcome)}; cohort = first {spec.time.grain or 'month'} an entity is active; "
                 "one aggregate row per (cohort, period), entity ids never leave the source",
                 "rows with NULL entity or period are excluded"]
        return sb.CompiledQuery(sb.to_sql(q, dialect), {"cohort": "cohort", "period": "period", "active": "active"},
                                notes, purpose, dialect, MAX_CELLS, "aggregate")

    def _tested(self, rows: Rows, spec: AnalysisSpec) -> tuple[list[dict], list[dict], list[str], dict]:
        allc, info = cohorts(rows["primary"], spec)
        kept, warns, sel = select_segments([c for c in allc if c["observed"] and c["n"] > 0], spec)
        kept.sort(key=lambda c: c["segment"])
        if info["censored_cohorts"]:
            warns.append(f"{info['censored_cohorts']} most recent cohort(s) excluded: their next {info['grain']} is not observed yet")
        return allc, kept, warns, info | sel

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        allc, kept, warns, info = self._tested(rows, spec)
        stat = st.chi_square_rates(kept, alpha=alpha)
        stat.test = "chi_square_retention_by_cohort"
        stat.warnings = warns + stat.warnings
        stat.groups.sort(key=lambda g: g["segment"])
        entity = sb.describe(spec.outcome)
        stat.details.update(info | {"entity": entity, "horizon": HORIZON})
        stat.highlights.update({"entity": entity, "grain": info["grain"], "horizon": HORIZON, "n_cohorts": len(kept)})
        cols = ["cohort", "cohort_size"] + [f"retention_{k}" for k in range(1, CURVE + 1)]
        table = {"columns": cols, "rows": [[c["segment"], c["n"]] + [json_value(st._f(c["curve"].get(k)))
                                                                     for k in range(1, CURVE + 1)] for c in allc]}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        _, kept, _, _ = self._tested(rows, spec)
        top, base = primary.highlights.get("top_segment"), primary.highlights.get("baseline_segment")
        segs = {c["segment"] for c in kept}
        if len(kept) < 2 or top not in segs or base not in segs or top == base:
            return nothing_to_verify(spec, primary, "grouped_logistic_regression",
                                     "primary best/worst cohorts unavailable; nothing to verify", sum(c["n"] for c in kept))
        gl = st.grouped_logistic(kept, baseline=base, alpha=alpha)
        perm = st.permutation_chi_square(kept, n_perm=2000, seed=0)
        o = gl["odds_ratios"][top]
        lr_p, perm_p = gl["lr_p_value"], perm["p_value"]
        direction = o["odds_ratio"] is not None and o["odds_ratio"] > 1
        supported = (st._sig(lr_p, alpha) and st._sig(perm_p, alpha) and o["ci_low"] is not None and o["ci_low"] > 1
                     and o["odds_ratio"] >= st.MIN_ODDS_RATIO)
        v = StatResult(method=spec.method, test="grouped_logistic_regression+permutation_chi_square",
                       n=sum(c["n"] for c in kept), statistic=gl["lr_statistic"], p_value=lr_p, effect_size=o["odds_ratio"],
                       effect_label="odds_ratio_best_vs_worst_cohort", ci_low=o["ci_low"], ci_high=o["ci_high"],
                       groups=[{"segment": s, **d} for s, d in gl["odds_ratios"].items()],
                       highlights={"top_segment": top, "baseline_segment": base, "odds_ratio": st._q(o["odds_ratio"]),
                                   "lr_p_value": lr_p, "permutation_p_value": perm_p},
                       warnings=gl["warnings"], supported=supported,
                       details={"permutation": perm, "alpha": alpha, "threshold_odds_ratio": st.MIN_ODDS_RATIO})
        table = {"columns": ["cohort", "odds_ratio", "ci_low", "ci_high", "p_value"],
                 "rows": [[s, d["odds_ratio"], d["ci_low"], d["ci_high"], d["p_value"]] for s, d in gl["odds_ratios"].items()]}
        return finish(spec, primary, v, direction, table)

    def claim_subject(self, spec: Mapping[str, Any]) -> Any:
        t = spec.get("time") or {}
        return f"cohort:{t.get('column')}:{t.get('grain')}"

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        if "top_rate" not in hl:
            return None
        entity = (spec.get("outcome") or {}).get("label") or (spec.get("outcome") or {}).get("column") or "entities"
        grain = hl.get("grain") or (spec.get("time") or {}).get("grain") or "period"
        title = f"Retention differs by cohort: the {hl.get('top_segment')} cohort retains best"
        text = (f"The {hl.get('top_segment')} cohort kept {fmt_pct(hl['top_rate'])} of its {entity} into the next {grain}, "
                f"versus {fmt_pct(hl.get('baseline_rate', 0))} for the {hl.get('baseline_segment')} cohort.")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="trend", dimension="cohort", measure="outcome")
