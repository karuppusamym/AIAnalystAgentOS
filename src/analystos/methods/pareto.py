"""pareto: concentration of volume (count, or the sum of an outcome) across segments.

primary       top-k share, top-20% share, Gini and a chi-square goodness-of-fit vs uniform
verification  multinomial bootstrap of the top segment's share (and of the top-20% share)
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from sqlglot import exp

from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.methods.base import (
    OTHER,
    AnalysisMethod,
    AnalysisOutcome,
    ChartIntent,
    ColumnType,
    Rows,
    Semantic,
    finish,
    fmt_pct,
    nothing_to_verify,
    seg_label,
    text_parts,
)
from analystos.skills import sqlbuild as sb
from analystos.skills import stats as st


def items(rows: list[dict[str, Any]], spec: AnalysisSpec) -> tuple[list[dict], dict]:
    null_seg = sum(int(r["n_rows"] or 0) for r in rows if r["segment"] is None)
    out = [{"segment": seg_label(r["segment"], spec.segment), "volume": float(r["volume"] or 0)}
           for r in rows if r["segment"] is not None]
    return out, {"excluded_null_segment_rows": null_seg, "truncated_segments": len(rows) >= sb.MAX_PARETO_SEGMENTS}


class Pareto(AnalysisMethod):
    name = "pareto"
    vocabulary = "concentration of volume across `segment` (optionally with filters)."
    segment_types = frozenset({"categorical", "boolean"})

    def applicable(self, outcome: ColumnType | None, segment: ColumnType | None) -> bool:
        return segment in (self.segment_types or ()) and outcome in (None, "numeric", "boolean")

    def validate(self, spec: AnalysisSpec, semantic: Semantic) -> list[str]:
        return [f"{self.name} needs a segment"] if spec.segment is None else []

    def compile(self, spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> sb.CompiledQuery:
        seg_cols, _ = sb.segment_cols(spec, dialect)
        cols = [seg_cols[0]]
        notes: list[str] = []
        if spec.outcome is not None:
            o = sb.derive(spec.outcome, dialect)
            cols.append(("outcome", o if spec.outcome.type in sb.BOOLEAN_DERIVATIONS else sb.as_double(o, dialect)))
            volume: exp.Expression = exp.Sum(this=sb.ref("outcome"))
            notes.append(f"volume = SUM({sb.describe(spec.outcome)})")
        else:
            volume = sb.count_star()
            notes.append("volume = COUNT(*)")
        inner = sb.base_select(spec, dialect, cols)
        q = (exp.select(sb.ref("segment"), sb.count_star().as_(sb.ident("n_rows")), volume.as_(sb.ident("volume")))
             .from_(sb.subquery(inner, "d")).group_by(sb.ref("segment"))
             .order_by(exp.Ordered(this=sb.ref("volume"), desc=True), sb.ref("segment")).limit(sb.MAX_PARETO_SEGMENTS))
        return sb.CompiledQuery(sb.to_sql(q, dialect), {"segment": "segment", "n_rows": "n_rows", "volume": "volume"},
                                notes, purpose, dialect, sb.MAX_PARETO_SEGMENTS, "aggregate")

    def test(self, rows: Rows, spec: AnalysisSpec, *, alpha: float = 0.05) -> AnalysisOutcome:
        its, excl = items(rows["primary"], spec)
        stat = st.pareto_concentration(its, alpha=alpha)
        stat.details.update(excl | {"segment": sb.describe(spec.segment)})
        stat.highlights["segment_by"] = sb.describe(spec.segment)
        top = stat.groups[: spec.top_k]
        out = [[g["segment"], g["volume"], g["share"], g["cumulative_share"]] for g in top]
        rest = stat.groups[spec.top_k:]
        if rest:
            out.append([OTHER, st._f(sum(g["volume"] for g in rest)), st._f(sum(g["share"] for g in rest)), 1.0])
        table = {"columns": ["segment", "volume", "share", "cumulative_share"], "rows": out}
        return AnalysisOutcome(method=spec.method, stat=stat, table=table)

    def verify(self, rows: Rows, spec: AnalysisSpec, primary: StatResult, *, alpha: float = 0.05) -> AnalysisOutcome:
        its, _ = items(rows["primary"], spec)
        its = [i for i in its if i["volume"] > 0]
        top = primary.highlights.get("top_segment")
        segs = [i["segment"] for i in its]
        if len(its) < 2 or top not in segs:
            return nothing_to_verify(spec, primary, "multinomial_bootstrap_top_share",
                                     "primary top segment unavailable; nothing to verify")
        vols = np.array([round(i["volume"]) for i in its], dtype=np.int64)
        total = int(vols.sum())
        p = vols / total
        rng = np.random.default_rng(0)
        sims = rng.multinomial(total, p, size=2000)
        ti = segs.index(top)
        shares = sims[:, ti] / total
        stability = float((sims.argmax(axis=1) == ti).mean())
        k = len(its)
        n20 = max(1, math.ceil(0.2 * k))
        top20 = np.sort(sims, axis=1)[:, ::-1][:, :n20].sum(axis=1) / total
        lo, hi = np.quantile(shares, [alpha / 2, 1 - alpha / 2])
        lo20 = float(np.quantile(top20, alpha / 2))
        fair = 1 / k
        supported = lo >= st.MIN_TOP_SHARE_RATIO * fair or lo20 >= st.MIN_TOP20_SHARE
        v = StatResult(method=spec.method, test="multinomial_bootstrap_top_share", n=total, statistic=st._f(p[ti]),
                       effect_size=st._f(p[ti] / fair), effect_label="top_share_vs_fair_share", ci_low=st._f(lo), ci_high=st._f(hi),
                       highlights={"top_segment": top, "top_share": st._q(p[ti]), "top_share_ci": [st._q(lo), st._q(hi)],
                                   "top_segment_stability": st._q(stability), "top_20pct_share_ci_low": st._q(lo20)},
                       assumptions=["records resampled with replacement (multinomial), 2000 resamples, seed 0"],
                       supported=supported, details={"alpha": alpha, "fair_share": st._f(fair)})
        table = {"columns": ["top_segment", "share", "ci_low", "ci_high", "stability"],
                 "rows": [[top, st._f(p[ti]), st._f(lo), st._f(hi), st._f(stability)]]}
        return finish(spec, primary, v, stability >= 0.5, table)

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        hl = stat.get("highlights") or {}
        if "top_share" not in hl and "top_k_share" not in hl:
            return None
        seg, _, scope = text_parts(spec)
        share = hl.get("top_share", hl.get("top_k_share"))
        title = f"Volume is concentrated in few {seg} values"
        text = (f"{hl.get('top_segment', 'The top segment')} accounts for {fmt_pct(share)} of records{scope}"
                + (f"; the top {hl.get('top_k')} account for {fmt_pct(hl['top_k_share'])}"
                   if hl.get("top_k_share") and hl.get("top_k") else "") + ".")
        return title, text

    def chart_intent(self, spec: AnalysisSpec, stat: Mapping[str, Any] | None = None) -> ChartIntent:
        return ChartIntent(intent="part_to_whole", dimension="segment", measure="volume")
