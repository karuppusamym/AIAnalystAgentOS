"""REV Critic Agent (§26, §28): Reason -> Evaluate -> Verify.

Verified == every deterministic check passes (method fit, sample size, adjusted significance,
effect size, reproducible re-run, independent second method). Model opinions (independent model
family + JEV) are recorded and can lower confidence or add caveats, but cannot make a finding true."""
from __future__ import annotations

import re

from sqlalchemy import select

from analystos.agents.common import llm_json, task_output
from analystos.agents.insight import template_text
from analystos.agents.investigator import with_constraints
from analystos.artifacts.registry import link
from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.core.errors import AnalystOSError
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Experiment, Hypothesis, Insight, QueryExecution
from analystos.events.bus import emit
from analystos.llm.config import family
from analystos.runtime.context import RunContext
from analystos.services.platform_settings import get as platform

CAUSAL = re.compile(r"\b(causes?|caused|drives?|driven by|because|leads? to|results? in|due to)\b", re.I)
MIN_N = 100


def _dump(v):
    return v.model_dump() if hasattr(v, "model_dump") else v


def representative_population(ctx: RunContext, asset: str) -> dict:
    """P4-C12: a claim is about the population sampled. Fails when the staged snapshot was truncated
    without a declared sample (or by first_n); records the sampling method either way."""
    from analystos.staging.snapshots import population_for

    return population_for(asset, ctx.scope.asset_sources.get(asset)).check()


def verify_insights(ctx: RunContext) -> dict:
    from analystos.skills.analysis import verify_analysis

    with session_scope() as s:
        insights = [(i.id, i.code) for i in s.scalars(select(Insight).where(Insight.run_id == ctx.run.id, Insight.status == "draft"))]
    quality = task_output(ctx.run.id, "quality").get("issues") or []
    verified_codes, failed_codes = [], []
    directions: dict[tuple, list] = {}
    for insight_id, code in insights:
        ctx.check_control()
        with session_scope() as s:
            ins = s.get(Insight, insight_id)
            h = s.get(Hypothesis, ins.hypothesis_id)
            exp = s.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.role == "primary"))
            originals = {q.id: (q.sql, q.result_hash) for q in s.scalars(select(QueryExecution).where(QueryExecution.id.in_(exp.query_ids)))}
            finding, spec_d, stat_d, narrative_source = ins.finding, dict(h.spec), dict(exp.result), ins.narrative_source
            statement = h.statement
        spec = with_constraints(AnalysisSpec.model_validate(spec_d), ctx.run.constraints)
        run_sql = ctx.run_sql(ctx.scope.asset_sources.get(spec.asset))
        alpha = ctx.policy.alpha
        # ---- Reason
        reason = {"claim": finding, "question": statement, "required_evidence": ["primary query result", f"{stat_d.get('test')} statistics",
                                                                                 "reproducible re-run", "independent second method"]}
        # ---- Evaluate
        checks = []
        groups = stat_d.get("groups") or []
        min_group = min((g.get("n") or 0 for g in groups), default=stat_d.get("n") or 0)
        checks.append({"check": "method_fit", "passed": not any("expected" in w.lower() and "<5" in w for w in stat_d.get("warnings") or []),
                       "detail": f"{stat_d.get('test')} for {spec.method}; assumptions: {', '.join(stat_d.get('assumptions') or []) or 'n/a'}"})
        min_n = max(MIN_N, platform().analysis.min_sample_size)  # the admin can raise the floor, never lower it
        checks.append({"check": "sample_size", "passed": (stat_d.get("n") or 0) >= min_n,
                       "detail": f"n={stat_d.get('n')}, smallest group={min_group} (min {min_n})"})
        p_adj = stat_d.get("p_adjusted", stat_d.get("p_value"))
        checks.append({"check": "significance_after_bh", "passed": p_adj is not None and p_adj < alpha, "detail": f"q={p_adj} alpha={alpha}"})
        checks.append({"check": "effect_size", "passed": bool(stat_d.get("supported")),
                       "detail": f"{stat_d.get('effect_label')}={stat_d.get('effect_size')}"})
        checks.append(representative_population(ctx, spec.asset))
        overreach = bool(CAUSAL.search(finding))
        if overreach:
            _, finding = template_text(stat_d, spec_d)
            narrative_source = "template"
        checks.append({"check": "no_overreach", "passed": True, "detail": "causal wording rewritten to association" if overreach else "associational wording"})
        dq = [q.get("message") for q in quality if q.get("severity") in ("warning", "critical") and
              any(c in str(q.get("column") or "") for c in [d.get("column") for d in (spec_d.get("outcome") or {}, spec_d.get("segment") or {}) if d])]
        # ---- Verify: reproducibility
        reproducible, repro_detail = True, []
        for qid, (sql, original_hash) in originals.items():
            try:
                again = ctx.services.gateway.execute(ctx.scope, sql, actor=f"agent:{ctx.agent.id}", purpose="verification.rerun",
                                                     run_id=ctx.run.id, task_id=ctx.task.id, use_cache=False)
                same = again.result_hash == original_hash
                reproducible &= same
                repro_detail.append(f"{qid}: {'identical' if same else 'DIFFERENT'} result hash")
            except AnalystOSError as exc:
                reproducible = False
                repro_detail.append(f"{qid}: re-run failed ({exc.code})")
        checks.append({"check": "reproducible_rerun", "passed": reproducible, "detail": "; ".join(repro_detail)})
        # ---- Verify: independent second method
        second = None
        try:
            second = ctx.tools().invoke("analysis.verify", {"insight": code, "method": spec.method},
                                        lambda spec=spec, run_sql=run_sql, stat_d=stat_d, alpha=alpha: verify_analysis(
                                            spec, run_sql, StatResult.model_validate(stat_d), alpha=alpha))
            sd = _dump(second.stat)
            agrees = bool(sd.get("details", {}).get("agrees", sd.get("supported")))
            checks.append({"check": "second_method", "passed": agrees, "detail": f"{sd.get('test')}: p={sd.get('p_value')} "
                           f"effect={sd.get('effect_size')} agrees={agrees}"})
        except AnalystOSError as exc:
            checks.append({"check": "second_method", "passed": False, "detail": f"failed: {exc.code}"})
        # ---- Verify: independent model family + JEV (recorded, not decisive)
        primary_family = family(narrative_source.split(":", 1)[1]) if narrative_source.startswith("llm:") else "anthropic"
        review, review_model = llm_json(ctx, "verification", "verification.v1",
                                        {"claim": finding, "hypothesis": statement, "method": spec.method,
                                         "statistics": {k: stat_d.get(k) for k in ("test", "n", "p_value", "p_adjusted", "effect_size",
                                                                                   "effect_label", "highlights", "warnings")},
                                         "checks": checks}, exclude_families=[primary_family])
        jev = ctx.jev.probability("rev_second_opinion", {"claim": finding, "evidence": str({k: stat_d.get(k) for k in (
            "test", "n", "p_adjusted", "effect_size", "effect_label", "highlights")})[:3000]},
            "Does `evidence` support `claim` as worded, without overreach?", ctx=ctx.call_ctx())
        # ---- Contradictions
        key = (spec.asset, (spec_d.get("outcome") or {}).get("column"), (spec_d.get("segment") or {}).get("column"))
        top = (stat_d.get("highlights") or {}).get("top_segment")
        contradiction = [c for c, t in directions.get(key, []) if t != top and spec.filters == []]
        directions.setdefault(key, []).append((code, top))
        deterministic_ok = all(c["passed"] for c in checks)
        conf = 0.5
        if p_adj is not None:
            conf += 0.15 if p_adj < 0.001 else 0.1 if p_adj < 0.01 else 0.0
        conf += 0.1 if any(c["check"] == "second_method" and c["passed"] for c in checks) else -0.2
        conf += 0.1 if reproducible else -0.3
        caveats_extra = []
        if isinstance(review, dict):
            conf += 0.05 if review.get("supports") else -0.1
            if review.get("suggested_caveat"):
                caveats_extra.append(f"Reviewer: {str(review['suggested_caveat'])[:200]}")
        if jev is not None:
            conf += 0.05 if jev.value >= 0.7 else -0.1 if jev.value < 0.3 else 0.0
        if dq:
            caveats_extra.append("Data quality: " + "; ".join(dq[:2]))
            conf -= 0.05
        if contradiction:
            caveats_extra.append(f"Contrasts with {', '.join(contradiction)} on the same outcome/segment.")
        conf = max(0.0, min(0.99, conf)) if deterministic_ok else min(conf, 0.4)
        verification = {"reason": reason, "evaluate": checks, "verify": {
            "reproducible": reproducible, "second_method": _dump(second.stat) if second else None,
            "independent_model": {"model": review_model, "review": review} if isinstance(review, dict) else {"unavailable": review_model},
            "jev": {"p_supports": jev.value, "model": jev.model} if jev else None,
            "contradictions": contradiction}, "verified": deterministic_ok,
            "note": "Verification is grounded in deterministic checks and reproducible data; model opinions only adjust confidence."}
        with session_scope() as s:
            ins = s.get(Insight, insight_id)
            ins.finding = finding
            ins.narrative_source = narrative_source
            ins.verified = deterministic_ok
            ins.verification = verification
            ins.confidence = round(conf, 3)
            ins.caveats = list(dict.fromkeys((ins.caveats or []) + caveats_extra))
            ins.status = "verified" if deterministic_ok else "failed_verification"
            if second is not None:
                vexp = Experiment(id=new_id("exp"), workspace_id=ctx.workspace.id, run_id=ctx.run.id, hypothesis_id=ins.hypothesis_id,
                                  method=spec.method, params={"verification_of": code}, result=_dump(second.stat),
                                  query_ids=list(second.query_ids), role="verification")
                s.add(vexp)
                link(s, ctx.workspace.id, ("insight", ins.id), "verified_by", ("experiment", vexp.id), run_id=ctx.run.id)
            if not deterministic_ok:
                h = s.get(Hypothesis, ins.hypothesis_id)
                h.status = "inconclusive"
            emit(ctx.workspace.id, "insight.verified", {"code": code, "verified": deterministic_ok, "confidence": ins.confidence,
                                                        "failed_checks": [c["check"] for c in checks if not c["passed"]]},
                 run_id=ctx.run.id, session=s)
        (verified_codes if deterministic_ok else failed_codes).append(code)
        ctx.say(f"REV {code}: {'VERIFIED' if deterministic_ok else 'FAILED'} (confidence {conf:.2f}); "
                + ", ".join(f"{c['check']}={'ok' if c['passed'] else 'fail'}" for c in checks), kind="decision")
    return {"verified": verified_codes, "failed": failed_codes}
