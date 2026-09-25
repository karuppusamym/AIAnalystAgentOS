"""Insight Analyst Agent (§13.13, §25): supported + multiple-testing-adjusted results -> findings.

Narratives are guarded: every number in model-written text must be one of the computed facts
(after rounding), otherwise the deterministic template text is used and the fallback is recorded."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from analystos.agents.common import llm_json, model_gate
from analystos.artifacts.registry import link
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Experiment, Hypothesis, Insight
from analystos.events.bus import emit
from analystos.runtime.context import RunContext
from analystos.staging.snapshots import population_for

_NUM = re.compile(r"(?<![A-Za-z_-])-?\d+(?:\.\d+)?")


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def facts_for(stat: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    hl = stat.get("highlights") or {}
    facts: dict[str, Any] = {"test": stat.get("test"), "n": stat.get("n"), "p_value_adjusted": stat.get("p_adjusted"),
                             "effect": {stat.get("effect_label") or "effect_size": stat.get("effect_size")}}
    for k, v in hl.items():
        if isinstance(v, float) and 0 <= v <= 1 and ("rate" in k or "share" in k):
            facts[k] = _fmt_pct(v)
        elif isinstance(v, float):
            facts[k] = round(v, 2)
        else:
            facts[k] = v
    facts["segment"] = (spec.get("segment") or {}).get("label") or (spec.get("segment") or {}).get("column")
    facts["outcome"] = (spec.get("outcome") or {}).get("label") or (spec.get("outcome") or {}).get("column")
    facts["filters"] = [f"{f['column']} {f['op']} {f.get('value')}" for f in spec.get("filters") or []]
    return facts


def _cap(text: str) -> str:
    """Capitalise the first letter only ('missed SLA' -> 'Missed SLA', not 'Missed sla')."""
    return text[:1].upper() + text[1:]


def template_text(stat: dict[str, Any], spec: dict[str, Any]) -> tuple[str, str]:
    """Deterministic title + finding built only from computed values."""
    hl = stat.get("highlights") or {}
    seg = (spec.get("segment") or {}).get("label") or (spec.get("segment") or {}).get("column") or "segment"
    out = (spec.get("outcome") or {}).get("label") or (spec.get("outcome") or {}).get("column") or "volume"
    scope = f" (where {', '.join(f['column'] + ' ' + f['op'] + ' ' + str(f.get('value')) for f in spec.get('filters') or [])})" \
        if spec.get("filters") else ""
    m = spec.get("method")
    if m == "rate_by_segment" and "top_rate" in hl:
        title = f"{_cap(out)} concentrates in {seg} = {hl.get('top_segment')}"
        text = (f"Records with {seg} = {hl.get('top_segment')} have a {out} rate of {_fmt_pct(hl['top_rate'])} versus "
                f"{_fmt_pct(hl.get('baseline_rate', 0))} for {seg} = {hl.get('baseline_segment')}"
                + (f" ({hl['rate_ratio']:.1f}x)" if isinstance(hl.get("rate_ratio"), (int, float)) else "") + f"{scope}.")
    elif m == "numeric_by_segment" and ("top_median" in hl or "top_value" in hl):
        top = hl.get("top_median", hl.get("top_value"))
        base = hl.get("baseline_median", hl.get("baseline_value"))
        title = f"{_cap(out)} is higher for {seg} = {hl.get('top_segment')}"
        text = (f"Median {out} is {top:.1f} for {seg} = {hl.get('top_segment')} versus {base:.1f} for "
                f"{hl.get('baseline_segment')}" + (f" ({hl['ratio']:.1f}x)" if isinstance(hl.get("ratio"), (int, float)) else "") + f"{scope}.")
    elif m == "pareto" and ("top_share" in hl or "top_k_share" in hl):
        share = hl.get("top_share", hl.get("top_k_share"))
        title = f"Volume is concentrated in few {seg} values"
        text = (f"{hl.get('top_segment', 'The top segment')} accounts for {_fmt_pct(share)} of records{scope}"
                + (f"; the top {hl.get('top_k')} account for {_fmt_pct(hl['top_k_share'])}" if hl.get("top_k_share") and hl.get("top_k") else "") + ".")
    elif m == "trend":
        title = f"{_cap(out) if out != 'volume' else 'Volume'} shows a significant trend"
        text = (f"Weekly {out} changed by {hl.get('pct_change', 0):.1f}% from first to last period" if isinstance(hl.get("pct_change"), (int, float))
                else f"A significant trend was detected in {out}") + f"{scope}."
    elif m == "driver_model" and hl.get("top_driver"):
        title = f"{_cap(out)} is driven mainly by {hl['top_driver']}"
        feature, odds = hl.get("strongest_feature"), hl.get("strongest_odds_ratio")
        text = (f"Among the drivers tested, {hl['top_driver']} explains the most of {out}"
                + (f"; {feature} has {odds:.1f}x the odds" if feature and isinstance(odds, (int, float)) else "")
                + (f" (holdout AUC {hl['holdout_roc_auc']:.2f})" if isinstance(hl.get("holdout_roc_auc"), (int, float)) else "")
                + f"{scope}.")
    else:
        title = f"{_cap(out)} is associated with {seg}"
        text = f"{stat.get('test')} indicates an association (effect {stat.get('effect_size')}, n={stat.get('n')}){scope}."
    return title[:200], text


def _guard(text: str, facts: dict[str, Any]) -> bool:
    allowed: set[float] = set()
    for v in _flatten(facts):
        for n in _NUM.findall(str(v)):
            f = float(n)
            allowed |= {round(f, 0), round(f, 1), round(f, 2), f}
            if 0 <= f <= 1:
                allowed |= {round(f * 100, 0), round(f * 100, 1)}
    for n in _NUM.findall(text):
        f = float(n)
        if f in (1, 2, 3) or any(abs(f - a) < 0.051 for a in allowed):
            continue
        return False
    return True


def _flatten(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _flatten(v)
    elif isinstance(value, list):
        for v in value:
            yield from _flatten(v)
    else:
        yield value


def benjamini_hochberg(pvals: list[float]) -> list[float]:
    from analystos.skills.stats import benjamini_hochberg as bh

    return list(bh(pvals))


def build_insights(ctx: RunContext) -> dict:
    with session_scope() as s:
        rows = []
        for h in s.scalars(select(Hypothesis).where(Hypothesis.run_id == ctx.run.id, Hypothesis.status != "superseded")
                           .order_by(Hypothesis.created_at)):
            exp = s.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.role == "primary"))
            if exp is not None:
                rows.append((h.id, exp.id))
    tested = [(hid, eid) for hid, eid in rows]
    with session_scope() as s:
        exps = {eid: s.get(Experiment, eid) for _, eid in tested}
        pvals = [(eid, exps[eid].result.get("p_value")) for _, eid in tested if exps[eid].result.get("p_value") is not None]
        adjusted = dict(zip([e for e, _ in pvals], benjamini_hochberg([p for _, p in pvals]), strict=False)) if pvals else {}
        for eid, adj in adjusted.items():
            exps[eid].result = {**exps[eid].result, "p_adjusted": float(adj)}
        candidates = []
        for hid, eid in tested:
            h = s.get(Hypothesis, hid)
            res = exps[eid].result
            adj = res.get("p_adjusted")
            if h.status == "supported" and adj is not None and adj >= ctx.policy.alpha:
                h.status = "inconclusive"
                h.conclusion = (h.conclusion or "") + f" — not significant after Benjamini-Hochberg adjustment (q={adj:.3g})"
            elif h.status == "supported":
                candidates.append((h.id, h.code, h.statement, dict(h.spec), dict(res), eid, list(exps[eid].query_ids), h.priority_score))
    candidates.sort(key=lambda c: (-(c[7] or 0), c[4].get("p_adjusted") or 1))
    # One finding per claim (method, outcome, segment/drivers, filters, top group): drill-downs that restate a
    # parent are merged. Carried-forward claims win ties so recurring analyses stay comparable.
    from analystos.services.changes import claim_key

    with session_scope() as s:
        carried = {h.id for h in s.scalars(select(Hypothesis).where(Hypothesis.run_id == ctx.run.id, Hypothesis.origin == "carried"))}
    seen_claims: dict[tuple, str] = {}
    unique = []
    for c in sorted(candidates, key=lambda c: (c[0] not in carried, len(c[3].get("filters") or []), -(c[4].get("effect_size") or 0))):
        claim = claim_key(c[3], c[4].get("highlights"))
        if claim in seen_claims:
            ctx.say(f"{c[1]} restates the finding of {seen_claims[claim]} (same claim); merged.", kind="decision")
            continue
        seen_claims[claim] = c[1]
        unique.append(c)
    candidates = sorted(unique, key=lambda c: (-(c[7] or 0), c[4].get("p_adjusted") or 1))
    created = []
    for hid, code, statement, spec, stat, eid, qids, _ in candidates:
        facts = facts_for(stat, spec)
        title, finding = template_text(stat, spec)
        source, action = "template", None
        payload = {"hypothesis": statement, "method": spec.get("method"), "facts": facts}
        data, model = llm_json(ctx, "insight_narrative", "insight_narrative.v1", payload) \
            if model_gate(ctx, "insight_narrative", payload, deterministic_ok=True) else (None, "deterministic")
        if isinstance(data, dict) and isinstance(data.get("finding"), str) and _guard(data["finding"] + " " + str(data.get("title", "")), facts):
            title, finding, source = str(data.get("title") or title)[:200], data["finding"], f"llm:{model}"
            action = data.get("recommended_action")
        elif data is not None:
            ctx.say(f"Narrative for {code} quoted numbers not present in the evidence; using the deterministic template.", kind="decision")
        hl = stat.get("highlights") or {}
        impact = {k: hl[k] for k in ("affected_records", "excess_events", "top_segment_n", "top_n") if k in hl}
        if "excess_events" not in impact and isinstance(hl.get("top_rate"), (int, float)) and isinstance(hl.get("baseline_rate"), (int, float)):
            n_top = next((g.get("n") for g in stat.get("groups") or [] if str(g.get("segment")) == str(hl.get("top_segment"))), None)
            if n_top:
                impact["affected_records"] = int(n_top)
                impact["excess_events"] = int(round((hl["top_rate"] - hl["baseline_rate"]) * n_top))
        caveats = ["Association in historical data; not proof of causation."] + list(stat.get("warnings") or [])[:3]
        if spec.get("filters"):
            caveats.append("Scoped to: " + ", ".join(f"{f['column']} {f['op']} {f.get('value')}" for f in spec["filters"]))
        population = population_for(spec.get("asset"), ctx.scope.asset_sources.get(spec.get("asset") or "")).caveat()
        if population:  # a sampled or truncated snapshot: say which population the finding describes (P4-C12)
            caveats.append(population)
        ctx.check_control()  # never write findings into a plan that was replaced while we ran
        with session_scope() as s:
            n = s.query(Insight).filter(Insight.run_id == ctx.run.id).count() + 1
            ins = Insight(id=new_id("ins"), workspace_id=ctx.workspace.id, run_id=ctx.run.id, hypothesis_id=hid, code=f"I-{n}",
                          title=title, finding=finding, confidence=0.0, population_size=int(stat.get("n") or 0),
                          business_impact={**impact, **({"recommended_action": action} if action else {})}, caveats=caveats,
                          evidence=[{"type": "hypothesis", "id": hid, "label": code}, {"type": "experiment", "id": eid, "label": stat.get("test")}]
                          + [{"type": "query", "id": q, "label": "evidence query"} for q in qids],
                          status="draft", narrative_source=source)
            s.add(ins)
            link(s, ctx.workspace.id, ("insight", ins.id), "supported_by", ("experiment", eid), run_id=ctx.run.id)
            link(s, ctx.workspace.id, ("hypothesis", hid), "concluded_as", ("insight", ins.id), run_id=ctx.run.id)
            emit(ctx.workspace.id, "insight.created", {"code": ins.code, "title": title}, run_id=ctx.run.id, session=s)
            created.append(ins.code)
    ctx.say(f"{len(created)} findings drafted from {len(candidates)} supported hypotheses "
            f"({len(tested)} tested; Benjamini-Hochberg applied across {len(pvals)} p-values).")
    return {"insights": created, "tested": len(tested), "supported": len(candidates)}
