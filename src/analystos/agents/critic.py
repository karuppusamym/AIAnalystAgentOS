"""REV Critic Agent (§26, §28): Reason -> Evaluate -> Verify.

Verified == every deterministic check passes (method fit, sample size, adjusted significance,
effect size, reproducible re-run, independent second method, and since P4-03: the narrative binds to
the typed facts, the data version did not change during the run, and the evidence bundle is complete).
`verified` says the REV checks passed; it does not say the claim is confirmed: the bundle's validation
state (`evidence.bundle`) is `exploratory` for a discovery until a confirmation rule
(`evidence.confirmation`) passes. Model opinions (independent model family + JEV) are recorded and can
lower the review score or add caveats, but cannot make a finding true; the score is uncalibrated."""
from __future__ import annotations

import re

from sqlalchemy import select

from analystos import methods
from analystos.agents.common import compact_json, llm_json, task_output
from analystos.agents.insight import template_text
from analystos.agents.investigator import with_constraints
from analystos.artifacts.registry import link
from analystos.contracts.analysis import AnalysisSpec, StatResult
from analystos.contracts.evidence import DataManifest, Fact
from analystos.core.errors import AnalystOSError
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Experiment, Hypothesis, Insight, QueryExecution, by_code
from analystos.decisions import Question
from analystos.events.bus import emit
from analystos.evidence.bundle import assemble
from analystos.evidence.confirmation import evaluate as confirm
from analystos.evidence.confirmation import is_replication, prior_claim
from analystos.evidence.facts import bind_finding
from analystos.evidence.manifest import changed as manifest_changed
from analystos.evidence.manifest import current_entry
from analystos.knowledge.attested import sql_hash
from analystos.llm.cache import estimate_tokens
from analystos.llm.config import family
from analystos.registries.hypotheses import spec_hash
from analystos.runtime.context import RunContext
from analystos.services.platform_settings import get as platform

CAUSAL = re.compile(r"\b(causes?|caused|drives?|driven by|because|leads? to|results? in|due to)\b", re.I)
MIN_N = 100


def _dump(v):
    return v.model_dump() if hasattr(v, "model_dump") else v


def narrative_family(narrative_source: str | None) -> str | None:
    """Model family that wrote the finding's wording (``llm:<provider>/<model>``), which the
    independent reviewer must not share. Template wording has no author model, so no family is
    excluded; guessing one would only narrow the reviewer pool for no independence gain."""
    if not narrative_source or not narrative_source.startswith("llm:"):
        return None
    model = narrative_source.split(":", 1)[1].strip()
    if not model or model == "deterministic":
        return None
    return family(model)


def representative_population(ctx: RunContext, asset: str) -> dict:
    """P4-C12: a claim is about the population sampled. Fails when the staged snapshot was truncated
    without a declared sample (or by first_n); records the sampling method either way."""
    from analystos.staging.snapshots import population_for

    return population_for(asset, ctx.scope.asset_sources.get(asset)).check()


def verify_insights(ctx: RunContext) -> dict:
    from analystos.skills.analysis import verify_analysis

    with session_scope() as s:
        insights = [(i.id, i.code) for i in s.scalars(select(Insight).where(Insight.run_id == ctx.run.id, Insight.status == "draft")
                                                          .order_by(*by_code(Insight.code)))]
    quality = task_output(ctx.run.id, "quality").get("issues") or []
    verified_codes, failed_codes = [], []
    directions: dict[tuple, list] = {}
    for insight_id, code in insights:
        ctx.check_control()
        with session_scope() as s:
            ins = s.get(Insight, insight_id)
            h = s.get(Hypothesis, ins.hypothesis_id)
            exp = s.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.role == "primary"))
            queries = list(s.scalars(select(QueryExecution).where(QueryExecution.id.in_(exp.query_ids))))
            originals = {q.id: (q.sql, q.result_hash, q.source_id) for q in queries}
            receipts = [{"query_id": q.id, "role": "primary", "query_hash": sql_hash(q.executed_sql or q.sql),
                         "result_hash": q.result_hash, "fingerprint": q.fingerprint, "rows": q.row_count} for q in queries]
            finding, spec_d, stat_d, narrative_source = ins.finding, dict(h.spec), dict(exp.result), ins.narrative_source
            statement, title, draft, prior_caveats = h.statement, ins.title, dict(ins.evidence_bundle or {}), list(ins.caveats or [])
            origin, iteration, parent = h.origin, h.iteration, h.parent_id
            recorded_manifest = dict(s.get(AnalysisRun, ctx.run.id).data_manifest or {})
            prior = prior_claim(s, ctx.workspace.id, ctx.run.id, spec_d)
        spec = with_constraints(AnalysisSpec.model_validate(spec_d), ctx.run.constraints)
        primary_family = narrative_family(narrative_source)  # before any rewrite below: who wrote the claim
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
        population = representative_population(ctx, spec.asset)
        checks.append(population)
        overreach = bool(CAUSAL.search(finding))
        if overreach:
            title, finding = template_text(stat_d, spec_d)
            narrative_source = "template"
        checks.append({"check": "no_overreach", "passed": True, "detail": "causal wording rewritten to association" if overreach else "associational wording"})
        # ---- Evaluate (P4-03): the final wording binds to the typed facts; the data version held still
        facts = [Fact.model_validate(f) for f in (draft.get("claim") or {}).get("facts") or []]
        binding = bind_finding(spec_d, stat_d, facts, title, finding)
        checks.append({"check": "fact_binding", "passed": bool(facts) and binding.ok,
                       "detail": ((f"{len(binding.mentions)} number(s) bound to {len(facts)} facts" if binding.ok
                                   else "; ".join(binding.problems[:4])) if facts else "no typed facts recorded")})
        with session_scope() as s:
            entry = current_entry(s, spec.asset, ctx.scope.asset_sources.get(spec.asset))
        recorded = DataManifest.model_validate(recorded_manifest).entry(spec.asset) if recorded_manifest else None
        moved = manifest_changed(recorded, entry) if recorded is not None else None
        checks.append({"check": "data_version_stable", "passed": recorded is not None and moved is None,
                       "detail": moved or ("no data-version manifest recorded for this run" if recorded is None else
                                           f"{entry.mode} {entry.version_basis} version {(entry.version or 'unversioned')[:12]}")})
        dq = [q.get("message") for q in quality if q.get("severity") in ("warning", "critical") and
              any(c in str(q.get("column") or "") for c in [d.get("column") for d in (spec_d.get("outcome") or {}, spec_d.get("segment") or {}) if d])]
        # ---- Verify: reproducibility
        reproducible, repro_detail = bool(originals), [] if originals else ["no query records: nothing to re-run"]
        for qid, (sql, original_hash, source_id) in originals.items():
            try:
                # Same budgeted, tool-gated path as every other run statement (P4-C02); no cache, or
                # the "re-run" would only replay the stored result.
                rerun = ctx.run_sql(source_id or ctx.scope.asset_sources.get(spec.asset))
                again = rerun(sql, purpose="verification.rerun", use_cache=False)
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
        # ---- Verify: independent model family (policy opt-in, P4-T02) + JEV (recorded, not decisive)
        review_payload = {"claim": finding, "hypothesis": statement, "method": spec.method,
                          "statistics": {k: stat_d.get(k) for k in ("test", "n", "p_value", "p_adjusted", "effect_size",
                                                                    "effect_label", "highlights", "warnings")},
                          "checks": checks}
        if ctx.policy.independent_model_verification:
            review, review_model = llm_json(ctx, "verification", "verification.v1", review_payload,
                                            exclude_families=[primary_family] if primary_family else [])
        else:
            review, review_model = None, "policy_off"
            ctx.router.record_skip("verification", ctx.call_ctx(), estimated_tokens=estimate_tokens(compact_json(review_payload)) + 500,
                                   reason="workspace policy: independent-model verification is opt-in; deterministic REV checks decide")
        # ADR-0015 escalate_only: the second opinion can add doubt (lower confidence), never add credit.
        second_opinion = ctx.decisions.decide(
            "rev_second_opinion", {"claim": finding, "evidence": str({k: stat_d.get(k) for k in (
                "test", "n", "p_adjusted", "effect_size", "effect_label", "highlights")})[:3000]},
            Question.escalation("Does `evidence` support `claim` as worded, without overreach?", levels=["none", "doubt"],
                                baseline="none", escalate_to="doubt", escalate_at=0.3, escalate_when="low"),
            ctx=ctx.call_ctx(), subject=f"insight:{insight_id}")
        jev = second_opinion if second_opinion.by_model and second_opinion.p is not None else None
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
        if second_opinion.value == "doubt":
            conf -= 0.1
        if dq:
            caveats_extra.append("Data quality: " + "; ".join(dq[:2]))
            conf -= 0.05
        if contradiction:
            caveats_extra.append(f"Contrasts with {', '.join(contradiction)} on the same outcome/segment.")
        # ---- Evidence bundle (P4-03): dimensions, missing-evidence refusal, discovery vs confirmation
        hl = stat_d.get("highlights") or {}
        pair = next((f for f in facts if f.primary), None)
        top = hl.get("top_segment", hl.get("top_driver"))
        direction = pair.direction if pair is not None else None
        method_impl = methods.get(spec.method) if spec.method in methods.names() else None
        confirmation = confirm(verified=deterministic_ok, origin=origin, top=top, direction=direction, prior=prior,
                               current=recorded or entry, holdout=(stat_d.get("details") or {}).get("holdout"))
        bundle = assemble(
            spec=spec_d, stat=stat_d, second=_dump(second.stat) if second is not None else None, facts=facts,
            binding=binding, rev_checks=checks, receipts=receipts, manifest=recorded_manifest, entry=recorded or entry,
            population=population, caveats=list(dict.fromkeys(prior_caveats + caveats_extra)), confirmation=confirmation,
            replicated=deterministic_ok and is_replication(origin, prior, recorded or entry, top),
            reproducible=reproducible, review_score=None, family_size=int(stat_d.get("bh_family_size") or 0),
            alpha=alpha, origin=origin, iteration=iteration, parent=parent,
            claim_meta={"subject": top, "baseline": hl.get("baseline_segment"), "direction": direction,
                        "metric": (spec_d.get("outcome") or {}).get("label") or (spec_d.get("outcome") or {}).get("column"),
                        "spec_hash": spec_hash(spec_d), "title": title, "text": finding,
                        "rendered_from": narrative_source},
            optional=tuple(getattr(method_impl, "evidence_optional", ()) or ()),
            predictive=isinstance(hl.get("holdout_roc_auc"), int | float))
        if bundle.validation.missing_evidence:
            deterministic_ok = False
            ctx.say(f"REV {code}: refused, missing evidence: {', '.join(bundle.validation.missing_evidence)}.", kind="decision")
        checks.append({"check": "evidence_complete", "passed": not bundle.validation.missing_evidence,
                       "detail": ", ".join(bundle.validation.missing_evidence) or "all required dimensions present"})
        conf = max(0.0, min(0.99, conf)) if deterministic_ok else min(conf, 0.4)
        bundle.review_score.value = round(conf, 3)
        verification = {"reason": reason, "evaluate": checks, "verify": {
            "reproducible": reproducible, "second_method": _dump(second.stat) if second else None,
            "independent_model": {"model": review_model, "review": review} if isinstance(review, dict) else {"unavailable": review_model},
            "jev": {"p_supports": jev.p, "model": jev.model, "by": jev.backend, "decision_id": jev.id} if jev else None,
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
            ins.title = title[:200]
            ins.evidence_bundle = bundle.model_dump(mode="json")
            ins.validation = bundle.validation.state
            ins.data_version = (bundle.data.get("manifest") or {}).get("version") or ins.data_version
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
                                                        "validation": bundle.validation.state, "label": bundle.validation.label,
                                                        "failed_checks": [c["check"] for c in checks if not c["passed"]]},
                 run_id=ctx.run.id, session=s)
        (verified_codes if deterministic_ok else failed_codes).append(code)
        ctx.say(f"REV {code}: {'VERIFIED' if deterministic_ok else 'FAILED'} ({bundle.validation.state}; review score "
                f"{conf:.2f}, uncalibrated); "
                + ", ".join(f"{c['check']}={'ok' if c['passed'] else 'fail'}" for c in checks), kind="decision")
    return {"verified": verified_codes, "failed": failed_codes}
