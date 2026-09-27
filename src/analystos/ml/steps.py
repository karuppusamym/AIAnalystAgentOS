"""Behaviours of `agent.ml_engineer` in `playbook.train` and `playbook.score` (P5-06). Deterministic: no
model call. They run the same service functions as the API, with the run's scope, query budget (through
`ctx.run_sql`) and plan hash.

* ``train_step``       trains `run.origin.ml_definition` (a published ml_spec). Without one, the ml_practitioner's
                       best proposal is saved as a *draft* ml_spec for a person to review and publish: a proposal
                       never trains by itself.
* ``score_plan_step``  plans `run.origin.scoring_definition` and requests its approval (hash-bound, incl. the plan
                       hash); a duplicate input requests nothing and the scoring step is skipped.
* ``score_step``       the side effect: executes the approved scoring run.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from analystos.core.errors import InvalidInput
from analystos.db.base import session_scope

SCORE_STEP = "ml_score"


def _requester(ctx: Any) -> Any:
    from analystos.db.models import User

    with session_scope() as s:
        user = s.get(User, ctx.run.requested_by)
        s.expunge(user)
    return user


def _best_proposal(ctx: Any) -> dict[str, Any] | None:
    from analystos.db.models import Artifact

    with session_scope() as s:
        arts = s.scalars(select(Artifact).where(Artifact.run_id == ctx.run.id, Artifact.type == "agent_output")
                         .order_by(Artifact.created_at.desc()))
        for art in arts:
            for r in (art.content or {}).get("results") or []:
                if str(r.get("capability", "")).startswith("skill.ml_propose_spec@") and (r.get("result") or {}).get("best"):
                    return r["result"]["best"]
    return None


def train_step(ctx: Any) -> dict[str, Any]:
    from analystos.contracts.definition import DefinitionDraftIn
    from analystos.contracts.work import MLSpec
    from analystos.services import definitions as defs
    from analystos.services.definitions import resolve_runnable
    from analystos.services.ml import start_experiment

    user = _requester(ctx)
    origin = ctx.run.origin or {}
    definition = origin.get("ml_definition")
    if not definition:
        proposal = _best_proposal(ctx)
        if proposal is None:
            raise InvalidInput("no ml_definition in the run and no valid proposal to save; publish an ml_spec definition")
        spec = MLSpec.model_validate(proposal)  # validated again: a proposal is data, never trusted
        key = f"proposal_{ctx.run.id.split('_', 1)[-1]}"
        with session_scope() as s:
            row = defs.create_draft(s, s.merge(user), ctx.workspace.id, DefinitionDraftIn(
                kind="ml_spec", key=key, title=f"Proposed by run {ctx.run.id}", spec=spec.model_dump(mode="json")))
            draft = {"id": row.id, "key": row.key, "version": row.version}
        ctx.say(f"Saved the proposed MLSpec as draft ml_spec {key}; nothing trains until a person reviews and publishes it.",
                kind="decision")
        return {"status": "proposal_saved", "draft": draft}
    with session_scope() as s:
        ref, raw = resolve_runnable(s, ctx.workspace.id, {"kind": "ml_spec", **definition} if isinstance(definition, dict)
                                    else definition, trigger="playbook.train")
    asset = (raw.get("dataset") or {}).get("asset")
    source_id = ((raw.get("dataset") or {}).get("source_id")) or ctx.scope.asset_sources.get(asset)
    ctx.check_control()
    result = start_experiment(user, ctx.workspace.id, ref.id,
                              run_id=ctx.run.id, runner=ctx.run_sql(source_id), scope=ctx.scope)
    ctx.say(f"Experiment {result['id']}: {result['verdict']} ({result['summary'].get('decision', {}).get('reason', '')}).",
            kind="decision")
    return {"experiment_id": result["id"], "verdict": result["verdict"],
            "model_version_id": (result.get("model_version") or {}).get("id")}


def score_plan_step(ctx: Any) -> dict[str, Any]:
    from analystos.db.models import RunTask
    from analystos.services.definitions import resolve_runnable
    from analystos.services.ml import plan_scoring

    user = _requester(ctx)
    definition = (ctx.run.origin or {}).get("scoring_definition")
    if not definition:
        raise InvalidInput("a score run needs origin.scoring_definition (a published ml_scoring definition)")
    with session_scope() as s:
        ref, raw = resolve_runnable(s, ctx.workspace.id, {"kind": "ml_scoring", **definition} if isinstance(definition, dict)
                                    else definition, trigger="playbook.score")
    asset = (raw.get("input") or {}).get("asset")
    runner = ctx.run_sql(ctx.scope.asset_sources.get(asset)) if asset else None
    out = plan_scoring(user, ctx.workspace.id, ref.id, run_id=ctx.run.id, plan_hash=ctx.run.plan_hash, runner=runner,
                       scope=ctx.scope)
    if out["status"] == "approval_required":
        with session_scope() as s:
            task = s.scalar(select(RunTask).where(RunTask.run_id == ctx.run.id, RunTask.key == SCORE_STEP))
            if task is not None:
                task.input = {**task.input, "approval_id": out["approval_id"], "scoring_run_id": out["scoring_run_id"]}
        ctx.say(f"Scoring planned ({out['scoring_run_id']}); nothing is written until approval {out['approval_id']} is "
                "granted.", kind="decision")
    else:
        ctx.say(f"Scoring not needed: {out.get('reason', out['status'])}", kind="decision")
    return out


def score_step(ctx: Any) -> dict[str, Any]:
    from analystos.services.ml import execute_scoring

    scoring_id, approval_id = ctx.task.input.get("scoring_run_id"), ctx.task.input.get("approval_id")
    if not scoring_id:
        raise InvalidInput("no scoring run was planned for this run")
    ctx.check_control()
    out = execute_scoring(_requester(ctx), scoring_id, ctx.workspace.id, approval_id=approval_id)
    run = out.get("scoring_run") or {}
    ctx.say(f"Scored {run.get('rows_scored')} row(s) into {run.get('output_table')} ({run.get('rows_rejected')} rejected).",
            kind="decision")
    return out
