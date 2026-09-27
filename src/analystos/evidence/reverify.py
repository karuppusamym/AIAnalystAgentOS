"""Explicit re-verification (P7-01 follow-up, ADR-0020 decision 5): a new verdict on today's dependencies.

Re-verifying never edits the old record: a VOID verdict stays VOID and readable with its cause, and the
new verdict is a new record written by the subject's own deterministic path:

* ``step``           a new version of the same spec re-executed through the gateway and self-checked
                     (`services/steps.rerun`); synchronous.
* ``insight``        a replay run of the finding's frozen `AnalysisSpec` (no planner, no model call) whose
                     REV writes the new verdict; returns the run to follow.
* ``ml_experiment``  a new experiment of the same published `ml_spec` version (`services/ml.start_experiment`),
                     split, fitted and evaluated again; synchronous.

Only a subject's latest record can be re-verified, and only when it is not already current: an ACTIVE
record whose dependencies still match is refused (nothing to re-verify). An ACTIVE record whose
dependency changed without an event is voided first, with its cause, exactly as the sweep would.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from analystos.core.errors import Conflict, InvalidInput, NotFound
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import User

log = get_logger(__name__)
SUBJECTS = ("step", "insight", "ml_experiment")


def _stale_dependencies(session: Any, rec: Any) -> list[dict[str, Any]]:
    from analystos.evidence.verification import UNKNOWABLE, current_version

    out = []
    for d in rec.dependencies or []:
        cur = current_version(session, d["kind"], d["ref"])
        if cur != UNKNOWABLE and cur != d["version_hash"]:
            out.append({**d, "current": cur})
    return out


def _prepare(user: User, record_id: str) -> dict[str, Any]:
    """Check the record can be re-verified; void it first when a dependency moved without an event."""
    from analystos.db.models import VerificationRecord
    from analystos.evidence.verification import LIVE, latest, state_of, void_dependents
    from analystos.governance.policy import require_role

    with session_scope() as s:
        rec = s.get(VerificationRecord, record_id)
        if rec is None:
            raise NotFound("verification record not found")
        require_role(s, user, rec.workspace_id, "analyst")
        if rec.subject_type not in SUBJECTS:
            raise InvalidInput(f"a {rec.subject_type} verdict is re-verified by re-running what produced it; "
                               f"this action re-verifies {', '.join(SUBJECTS)}")
        newest = latest(s, rec.subject_type, [rec.subject_id]).get(rec.subject_id)
        if newest is not None and newest.id != rec.id:
            raise Conflict(f"record {rec.id} is not the latest verdict of {rec.subject_type} {rec.subject_id} "
                           f"(latest: {newest.id}, {newest.state}); re-verify the latest one",
                           details={"latest_record_id": newest.id, "latest_state": newest.state})
        if rec.state in LIVE:
            stale = _stale_dependencies(s, rec)
            if not stale:
                raise Conflict(f"record {rec.id} is {rec.state} and every dependency is current; nothing to re-verify",
                               details={"record_id": rec.id, "state": rec.state})
            d = stale[0]
            void_dependents(s, d["kind"], d["ref"], d["current"], f"{d['kind']} {d['ref']} changed since the verdict",
                            event="verification.reverify_requested")
            s.flush()
            s.refresh(rec)
        return {"record_id": rec.id, "workspace_id": rec.workspace_id, "run_id": rec.run_id,
                "subject_type": rec.subject_type, "subject_id": rec.subject_id, "previous": state_of(rec)}


def _insight(user: User, prep: dict[str, Any]) -> dict[str, Any]:
    from analystos.db.models import AnalysisRun, Hypothesis, Insight
    from analystos.registries.hypotheses import spec_hash
    from analystos.services.runs import create_run

    with session_scope() as s:
        ins = s.get(Insight, prep["subject_id"])
        h = s.get(Hypothesis, ins.hypothesis_id) if ins is not None and ins.hypothesis_id else None
        run = s.get(AnalysisRun, ins.run_id) if ins is not None else None
        if ins is None or h is None or run is None:
            raise NotFound("the finding's hypothesis or run no longer exists; it cannot be replayed")
        analysis = {"spec_hash": spec_hash(h.spec), "spec": dict(h.spec), "statement": h.statement, "question": h.question}
        sources = sorted(set(((run.scope or {}).get("asset_sources") or {}).values())) or None
        objective, code = run.objective, ins.code
    new = create_run(user, prep["workspace_id"], objective=objective, source_ids=sources, pins={"analyses": [analysis]},
                     origin={"type": "reverify", "record_id": prep["record_id"], "subject_type": "insight",
                             "subject_id": prep["subject_id"], "code": code, "previous_run_id": prep["run_id"],
                             "replay": True, "publish": "skip"})
    return {"status": "started", "run_id": new.id,
            "message": f"a replay run re-tests finding {code}'s frozen AnalysisSpec on today's data; its REV writes the "
                       "new verdict"}


def _step(user: User, prep: dict[str, Any]) -> dict[str, Any]:
    from analystos.services import steps

    out = steps.rerun(user, prep["subject_id"])
    step = out["step"]
    return {"status": "reverified", "step": step, "new_record": step.get("verification_record"),
            "rerun": [r.get("id") for r in out.get("rerun") or []]}


def _ml(user: User, prep: dict[str, Any]) -> dict[str, Any]:
    from analystos.db.models import MLExperiment
    from analystos.services import ml

    with session_scope() as s:
        exp = s.get(MLExperiment, prep["subject_id"])
        if exp is None:
            raise NotFound("the experiment no longer exists")
        definition = {"id": exp.definition_id} if exp.definition_id else \
            {"key": exp.definition_key, **({"version": exp.definition_version} if exp.definition_version else {})}
    out = ml.start_experiment(user, prep["workspace_id"], definition, run_id=prep["run_id"])
    with session_scope() as s:
        new = s.get(MLExperiment, out.get("id") or out.get("experiment_id"))
        verification = ml.verification_of(s, new) if new is not None else None
    return {"status": "reverified", "experiment": out, "new_record": verification}


HANDLERS = {"insight": _insight, "step": _step, "ml_experiment": _ml}


def reverify(user: User, record_id: str) -> dict[str, Any]:
    """Re-verify the subject of `record_id` by its own deterministic path; the old record is left as it was."""
    from analystos.db.models import VerificationRecord
    from analystos.events.bus import emit
    from analystos.governance.audit import audit

    prep = _prepare(user, record_id)
    out = HANDLERS[prep["subject_type"]](user, prep)
    new_record = (out.get("new_record") or {}).get("record_id")
    with session_scope() as s:
        old = s.get(VerificationRecord, record_id)
        payload = {"record_id": record_id, "subject_type": prep["subject_type"], "subject_id": prep["subject_id"],
                   "status": out["status"], "new_record_id": new_record, "run_id": out.get("run_id"),
                   "previous_state": old.state}
        emit(prep["workspace_id"], "verification.reverify_requested", payload, run_id=prep["run_id"],
             actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "verification.reverify", workspace_id=prep["workspace_id"], target=record_id,
              details=payload, session=s)
        still = s.scalar(select(VerificationRecord.state).where(VerificationRecord.id == record_id))
    return {"record_id": record_id, "subject_type": prep["subject_type"], "subject_id": prep["subject_id"],
            "previous": {**prep["previous"], "state_now": still}, **out}


__all__ = ["SUBJECTS", "reverify"]
