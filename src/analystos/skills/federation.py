"""Cross-source runs (P4-E03, INT-001..005): join keys across sources, then effects across the join.

Models propose, code decides. A join-key proposal may come from the rules below (declared
references and name heuristics across two sources), from a model or from a user; every one is
accepted only after the same deterministic checks, measured through the federated gateway runner:

* both columns exist in the scope's metadata for their assets, and the assets are in different sources;
* containment (share of non-null from-values found among the to-values) >= ``MIN_JOIN_CONTAINMENT``;
* the to-column is unique (``many_to_one`` or ``one_to_one``), so the join cannot fan out a measure.

A column the policy denies is rejected by the gateway validator inside the federated statement,
so a proposal naming one is rejected too, with the validator's reason.

``segment_effects_across`` then tests every numeric measure of the from-asset against every
low-cardinality attribute of the to-asset over the accepted join (rank tests from ``skills/stats``).
All SQL is built with ``skills/sqlbuild`` primitives and runs through ``run_sql`` (the gateway).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlglot import exp

from analystos.contracts.analysis import StatResult
from analystos.core.errors import SQLRejected
from analystos.skills.base import RunSQL
from analystos.skills.profiling import type_family
from analystos.skills.relationships import RelationshipCandidate, _candidates, measure_candidate
from analystos.skills.sqlbuild import _check_dialect, col, ident, not_null, table, to_sql
from analystos.skills.stats import compare_groups

MIN_JOIN_CONTAINMENT = 0.8
MAX_SEGMENTS = 12
MIN_GROUP = 20


class JoinProposal(BaseModel):
    from_asset: str
    from_column: str
    to_asset: str
    to_column: str
    origin: Literal["rule", "model", "user"] = "rule"
    rule: str | None = None  # declared | name_heuristic | same_name_key (rule proposals)


class JoinDecision(BaseModel):
    proposal: JoinProposal
    accepted: bool
    reason: str
    measured: RelationshipCandidate | None = None


class CrossSourceEffect(BaseModel):
    join: JoinProposal
    outcome: str  # from_asset.column
    segment: str  # to_asset.column
    stat: StatResult
    sql: str
    query_id: str


def _index(assets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {a["asset"]: a for a in assets}


def propose_cross_source_keys(assets: list[dict[str, Any]], asset_sources: dict[str, str]) -> list[JoinProposal]:
    """Rule proposals between assets of *different* sources (declared references, name heuristics)."""
    out: list[JoinProposal] = []
    for cand in _candidates(assets):
        fa, ta = cand["from"]["asset"], cand["to"]["asset"]
        if asset_sources.get(fa) and asset_sources.get(fa) != asset_sources.get(ta):
            out.append(JoinProposal(from_asset=fa, from_column=cand["col"]["name"], to_asset=ta,
                                    to_column=cand["to_col"]["name"], origin="rule", rule=cand["source"]))
    return out


def validate_join_keys(run_sql: RunSQL, proposals: list[JoinProposal], assets: list[dict[str, Any]],
                       asset_sources: dict[str, str], *, min_containment: float = MIN_JOIN_CONTAINMENT
                       ) -> list[JoinDecision]:
    """Accept or reject each proposal by metadata, containment and cardinality (see module docstring)."""
    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    idx = _index(assets)
    uniq_cache: dict[tuple[str, str], dict[str, Any]] = {}
    decisions: list[JoinDecision] = []
    for p in proposals:
        a, t = idx.get(p.from_asset), idx.get(p.to_asset)
        if a is None or t is None:
            decisions.append(JoinDecision(proposal=p, accepted=False, reason="asset not in the run scope"))
            continue
        if asset_sources.get(p.from_asset) == asset_sources.get(p.to_asset):
            decisions.append(JoinDecision(proposal=p, accepted=False, reason="both assets are in the same source"))
            continue
        c = next((x for x in a.get("columns", []) if x["name"] == p.from_column), None)
        tc = next((x for x in t.get("columns", []) if x["name"] == p.to_column), None)
        if c is None or tc is None:
            decisions.append(JoinDecision(proposal=p, accepted=False, reason="column not in the asset metadata"))
            continue
        try:
            measured = measure_candidate(run_sql, dialect, a, c, t, tc, p.rule or "declared", uniq_cache)
        except SQLRejected as exc:
            decisions.append(JoinDecision(proposal=p, accepted=False, reason=f"rejected by the gateway: {exc.message}"))
            continue
        if measured is None:
            decisions.append(JoinDecision(proposal=p, accepted=False, reason="the from-column has no values"))
            continue
        containment = measured.evidence["containment"]
        if containment < min_containment:
            reason, ok = f"containment {containment:.3f} < {min_containment}", False
        elif measured.cardinality in ("many_to_many", "one_to_many"):
            reason, ok = f"the to-column is not unique ({measured.cardinality}): the join would duplicate rows", False
        else:
            reason, ok = f"containment {containment:.3f}, {measured.cardinality}", True
        decisions.append(JoinDecision(proposal=p, accepted=ok, reason=reason, measured=measured))
    return decisions


def segment_effects_across(run_sql: RunSQL, join: JoinDecision, assets: list[dict[str, Any]], *,
                           alpha: float = 0.05, sample_rows: int = 50_000) -> list[CrossSourceEffect]:
    """Every numeric measure of the from-asset by every low-cardinality attribute of the to-asset,
    across an *accepted* join. Returns the tested results (supported or not), strongest first."""
    if not join.accepted:
        raise ValueError("segment_effects_across needs an accepted join decision")
    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    p = join.proposal
    idx = _index(assets)
    src, dst = idx[p.from_asset], idx[p.to_asset]
    measures = [c["name"] for c in src.get("columns", [])
                if type_family(c.get("data_type", "")) == "numeric" and not c.get("is_key")
                and c["name"] != p.from_column and not c.get("references")]
    attributes = [c["name"] for c in dst.get("columns", [])
                  if type_family(c.get("data_type", "")) in ("text", "boolean") and c["name"] != p.to_column]
    segments: list[str] = []
    for name in attributes:  # low cardinality only
        q = exp.select(exp.Count(this=exp.Distinct(expressions=[col(name)])).as_(ident("nd"))).from_(table(p.to_asset))
        try:
            r = run_sql(to_sql(q, dialect), purpose="federation.segment_cardinality", max_rows=1).records()
        except SQLRejected:  # a denied column: the gateway refuses it, so it is not a segment
            continue
        nd = int((r[0] if r else {}).get("nd") or 0)
        if 2 <= nd <= MAX_SEGMENTS:
            segments.append(name)
    out: list[CrossSourceEffect] = []
    for measure in measures:
        for seg in segments:
            f, d = exp.column(measure, table="f", quoted=True), exp.column(seg, table="d", quoted=True)
            q = (exp.select(d.copy().as_(ident("segment")), f.copy().as_(ident("value")))
                 .from_(table(p.from_asset, alias="f"))
                 .join(table(p.to_asset, alias="d"),
                       on=exp.EQ(this=exp.column(p.from_column, table="f", quoted=True),
                                 expression=exp.column(p.to_column, table="d", quoted=True)), join_type="inner")
                 .where(exp.and_(not_null(f.copy()), not_null(d.copy())))
                 .limit(sample_rows))
            sql = to_sql(q, dialect)
            res = run_sql(sql, purpose="federation.segment_effect", max_rows=sample_rows)
            groups: dict[str, list[float]] = {}
            for row in res.rows:
                groups.setdefault(str(row[0]), []).append(float(row[1]))
            groups = {k: v for k, v in groups.items() if len(v) >= MIN_GROUP}
            if len(groups) < 2:
                continue
            stat = compare_groups(groups, alpha=alpha)
            out.append(CrossSourceEffect(join=p, outcome=f"{p.from_asset}.{measure}", segment=f"{p.to_asset}.{seg}",
                                         stat=stat, sql=sql, query_id=res.query_id))
    out.sort(key=lambda e: (not e.stat.supported, -(abs(e.stat.effect_size or 0.0))))
    return out


class CrossSourceReport(BaseModel):
    proposals: list[JoinProposal] = Field(default_factory=list)
    decisions: list[JoinDecision] = Field(default_factory=list)
    effects: list[CrossSourceEffect] = Field(default_factory=list)


def investigate_cross_source(run_sql: RunSQL, assets: list[dict[str, Any]], asset_sources: dict[str, str], *,
                             extra_proposals: list[JoinProposal] | None = None, alpha: float = 0.05) -> CrossSourceReport:
    """Rule + external (model/user) proposals -> deterministic validation -> effects across accepted joins."""
    proposals = propose_cross_source_keys(assets, asset_sources) + list(extra_proposals or [])
    decisions = validate_join_keys(run_sql, proposals, assets, asset_sources)
    effects: list[CrossSourceEffect] = []
    seen: set[tuple[str, str, str, str]] = set()
    for d in decisions:
        key = (d.proposal.from_asset, d.proposal.from_column, d.proposal.to_asset, d.proposal.to_column)
        if d.accepted and key not in seen:
            seen.add(key)
            effects += segment_effects_across(run_sql, d, assets, alpha=alpha)
    effects.sort(key=lambda e: (not e.stat.supported, -(abs(e.stat.effect_size or 0.0))))
    return CrossSourceReport(proposals=proposals, decisions=decisions, effects=effects)
