"""Hypothesis registry (review C11, P4-T05).

Every hypothesis a run tested is registered per workspace under its spec hash, with the claim key
the method registry defines (`services.changes.claim_key`). A scheduled re-analysis replays the
registry instead of asking a model for hypotheses, so on unchanged data it asks the same questions
and gets the same answers: "new" and "resolved" then mean the evidence changed."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analystos.artifacts.registry import link
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import Experiment, Hypothesis, Insight, RegisteredHypothesis

TESTED = ("supported", "rejected", "inconclusive")
_NOT_IDENTITY = ("min_group_size", "top_k")  # same exclusions as investigator.identity_keys
_RESULT_KEYS = ("test", "n", "p_value", "p_adjusted", "effect_size", "effect_label")


def spec_hash(spec: dict[str, Any]) -> str:
    return stable_hash({k: v for k, v in spec.items() if k not in _NOT_IDENTITY})


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def register_run(session: Session, run_id: str) -> int:
    """Register (or refresh) every tested hypothesis of a run. Idempotent per run."""
    from analystos.services.changes import claim_key

    hyps = list(session.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id, Hypothesis.status.in_(TESTED))))
    if not hyps:
        return 0
    verified = set(session.scalars(select(Insight.hypothesis_id).where(Insight.run_id == run_id, Insight.status == "verified")))
    exps = {e.hypothesis_id: e for e in session.scalars(select(Experiment).where(Experiment.run_id == run_id,
                                                                                    Experiment.role == "primary"))}
    now = utcnow()
    # Rows are locked in spec-hash order: runs of one workspace finalizing at once register
    # overlapping hypotheses, and any other order deadlocks them (P4-S05, 50 concurrent runs).
    for h in sorted(hyps, key=lambda h: spec_hash(h.spec)):
        result = dict(exps[h.id].result or {}) if h.id in exps else {}
        claim = _plain(list(claim_key(h.spec, result.get("highlights"))))
        outcome = "verified" if h.id in verified else h.status
        last = {**{k: result.get(k) for k in _RESULT_KEYS}, "top": claim[-1] if claim else None}
        key = spec_hash(h.spec)
        session.execute(insert(RegisteredHypothesis).values(
            id=new_id("rhyp"), workspace_id=h.workspace_id, spec_hash=key, spec=h.spec, question=h.question or "",
            statement=h.statement, method=str(h.spec.get("method")), asset=str(h.spec.get("asset")),
            question_key=stable_hash(claim[:-1]), claim=claim, status="active", last_outcome=outcome, last_result=_plain(last),
            origin=h.origin or "agent", first_run_id=run_id, last_run_id="", times_tested=0, times_verified=0,
        ).on_conflict_do_nothing(index_elements=["workspace_id", "spec_hash"]))
        row = session.scalar(select(RegisteredHypothesis).where(RegisteredHypothesis.workspace_id == h.workspace_id,
                                                                RegisteredHypothesis.spec_hash == key).with_for_update())
        if row.last_run_id != run_id:
            row.times_tested += 1
            row.times_verified += int(outcome == "verified")
        row.last_run_id, row.last_outcome, row.last_result, row.claim = run_id, outcome, _plain(last), claim
        row.question_key, row.updated_at = stable_hash(claim[:-1]), now
        link(session, h.workspace_id, ("hypothesis", h.id), "registered_as", ("registered_hypothesis", row.id), run_id=run_id)
    return len(hyps)


def replay_proposals(session: Session, workspace_id: str, previous_run_id: str | None, assets: list[str], *,
                     scope: str = "previous_run") -> list[dict[str, Any]]:
    """The registered hypotheses a scheduled re-analysis re-tests, as investigator proposals.

    `previous_run` (default): the questions the previous run of this schedule tested, so the diff
    compares like with like. `workspace`: every active entry of the workspace on an asset in scope."""
    if previous_run_id:
        register_run(session, previous_run_id)  # a baseline from before the registry existed
    stmt = select(RegisteredHypothesis).where(RegisteredHypothesis.workspace_id == workspace_id,
                                              RegisteredHypothesis.status == "active")
    # Rows registered together share created_at and were inserted in spec-hash order; the id is random.
    rows = list(session.scalars(stmt.order_by(RegisteredHypothesis.created_at, RegisteredHypothesis.spec_hash)))
    if scope != "workspace":
        if not previous_run_id:
            return []
        wanted = {spec_hash(h.spec) for h in session.scalars(select(Hypothesis).where(
            Hypothesis.run_id == previous_run_id, Hypothesis.status.in_(TESTED)))}
        rows = [r for r in rows if r.spec_hash in wanted]
    allowed = set(assets)
    return [{"question": r.question, "statement": r.statement, "spec": dict(r.spec), "origin": "registry", "registry_id": r.id,
             "priority": "high" if r.last_outcome == "verified" else "medium",
             "priority_score": 1.0 if r.last_outcome == "verified" else 0.5,
             "rationale": f"Replay of registered hypothesis {r.id} (last outcome: {r.last_outcome or 'unknown'})"}
            for r in rows if r.asset in allowed]
