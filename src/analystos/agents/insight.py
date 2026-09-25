"""Insight Analyst Agent (§13.13, §25): supported + multiple-testing-adjusted results -> findings.

Narratives are guarded: every number in model-written text must be one of the computed facts
(after rounding), otherwise the deterministic template text is used and the fallback is recorded."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from analystos import methods
from analystos.agents.common import llm_json, model_gate
from analystos.artifacts.registry import link
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Experiment, Hypothesis, Insight
from analystos.events.bus import emit
from analystos.methods.base import cap, fmt_pct, text_parts
from analystos.runtime.context import RunContext
from analystos.staging.snapshots import population_for

_NUM = re.compile(r"(?<![A-Za-z_-])-?\d+(?:\.\d+)?")


def facts_for(stat: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    hl = stat.get("highlights") or {}
    facts: dict[str, Any] = {"test": stat.get("test"), "n": stat.get("n"), "p_value_adjusted": stat.get("p_adjusted"),
                             "effect": {stat.get("effect_label") or "effect_size": stat.get("effect_size")}}
    for k, v in hl.items():
        if isinstance(v, float) and 0 <= v <= 1 and ("rate" in k or "share" in k):
            facts[k] = fmt_pct(v)
        elif isinstance(v, float):
            facts[k] = round(v, 2)
        else:
            facts[k] = v
    facts["segment"] = (spec.get("segment") or {}).get("label") or (spec.get("segment") or {}).get("column")
    facts["outcome"] = (spec.get("outcome") or {}).get("label") or (spec.get("outcome") or {}).get("column")
    facts["filters"] = [f"{f['column']} {f['op']} {f.get('value')}" for f in spec.get("filters") or []]
    return facts


def template_text(stat: dict[str, Any], spec: dict[str, Any]) -> tuple[str, str]:
    """Deterministic title + finding built only from computed values: the method's own template
    (analystos.methods), or a generic association sentence when it has none for these highlights."""
    name = spec.get("method")
    method = methods.get(name) if name in methods.names() else None
    written = method.template_text(spec, stat) if method is not None else None
    if written is None:
        seg, out, scope = text_parts(spec)
        written = (f"{cap(out)} is associated with {seg}",
                   f"{stat.get('test')} indicates an association (effect {stat.get('effect_size')}, n={stat.get('n')}){scope}.")
    title, text = written
    return title[:200], text


def _language_ok(text: str, facts: dict[str, Any]) -> bool:
    """Model narrative must be in the platform's working language (English). Seen live: a low-cost
    model answered in Chinese and passed the numbers guard. Words quoted from the evidence (segment
    values may legitimately be in any script) are removed before measuring."""
    rest = text
    for v in _flatten(facts):
        if isinstance(v, str) and len(v) >= 2:
            rest = rest.replace(v, " ")
    letters = [ch for ch in rest if ch.isalpha()]
    if not letters:
        return True
    latin = sum(1 for ch in letters if ch.isascii() or "\u00c0" <= ch <= "\u024f")
    return latin / len(letters) >= 0.9


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
        text = (str(data.get("finding")) + " " + str(data.get("title", "")) + " " + str(data.get("recommended_action") or "")) \
            if isinstance(data, dict) else ""
        if isinstance(data, dict) and isinstance(data.get("finding"), str) and _guard(text, facts) and _language_ok(text, facts):
            title, finding, source = str(data.get("title") or title)[:200], data["finding"], f"llm:{model}"
            action = data.get("recommended_action")
        elif data is not None:
            why = "quoted numbers not present in the evidence" if not _guard(text, facts) else "was not written in English"
            ctx.say(f"Narrative for {code} {why}; using the deterministic template.", kind="decision")
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
