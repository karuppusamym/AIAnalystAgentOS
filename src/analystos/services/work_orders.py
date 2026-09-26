"""Typed work orders (workspace spec §3, P4-06): persist, edit under revision checks, start.

A work order is the envelope (`contracts.work.WorkOrderSpec`) around one typed payload. Only
`AnalysisWork` is executable today: starting it creates a run that replays exactly its AnalysisSpecs
(the frozen-set path pinned schedules use, ADR-0021), with no model call and no re-planning. The
placeholders (`PipelineSpec`, `MLSpec`, `ExperimentSpec`) validate and persist; starting one is
refused with `unsupported_capability` until P5/P6 give them executors.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.contracts.work import WorkOrderSpec
from analystos.core.errors import PreconditionFailed, UnsupportedCapability
from analystos.core.ids import new_id, stable_hash
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, User, WorkOrder
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader


def spec_hash(spec: WorkOrderSpec) -> str:
    return stable_hash(spec.model_dump(mode="json"))


def out(wo: WorkOrder) -> dict[str, Any]:
    d = {c: getattr(wo, c) for c in ("id", "workspace_id", "kind", "spec_type", "objective", "spec", "spec_hash", "revision",
                                      "status", "run_ids", "created_by")}
    d.update({c: getattr(wo, c).isoformat() if getattr(wo, c) else None for c in ("created_at", "updated_at")})
    d["executable"] = WorkOrderSpec.model_validate(wo.spec).executable
    return d


def create(session: Session, user: User, workspace_id: str, spec: WorkOrderSpec) -> WorkOrder:
    require_role(session, user, workspace_id, "analyst")
    wo = WorkOrder(id=new_id("wo"), workspace_id=workspace_id, kind=spec.kind, spec_type=spec.spec.type, objective=spec.objective,
                   spec=spec.model_dump(mode="json"), spec_hash=spec_hash(spec), revision=1, status="draft", run_ids=[],
                   created_by=user.id)
    session.add(wo)
    session.flush()
    emit(workspace_id, "work_order.created", {"work_order_id": wo.id, "kind": wo.kind, "type": wo.spec_type, "revision": 1},
         actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "work_order.created", workspace_id=workspace_id, target=wo.id,
          details={"kind": wo.kind, "type": wo.spec_type, "spec_hash": wo.spec_hash}, session=session)
    return wo


def _check(wo: WorkOrder, expected: int | None) -> None:
    if expected is not None and expected != wo.revision:
        raise PreconditionFailed(f"work order {wo.id} is at revision {wo.revision}, not {expected}",
                                 details={"current_revision": wo.revision})


def update(session: Session, user: User, wo: WorkOrder, spec: WorkOrderSpec, expected_revision: int | None) -> WorkOrder:
    """A new revision; runs already started keep the revision they ran (recorded in their origin)."""
    require_role(session, user, wo.workspace_id, "analyst")
    _check(wo, expected_revision)
    wo.kind, wo.spec_type, wo.objective = spec.kind, spec.spec.type, spec.objective
    wo.spec, wo.spec_hash = spec.model_dump(mode="json"), spec_hash(spec)
    wo.revision += 1
    emit(wo.workspace_id, "work_order.updated", {"work_order_id": wo.id, "revision": wo.revision}, actor=f"user:{user.id}",
         session=session)
    audit(f"user:{user.id}", "work_order.updated", workspace_id=wo.workspace_id, target=wo.id,
          details={"revision": wo.revision, "spec_hash": wo.spec_hash}, session=session)
    return wo


@scoped_loader
def start(user: User, workspace_id: str, work_order_id: str, *, expected_revision: int | None,
          idempotency: Any = None) -> tuple[AnalysisRun, bool]:
    from analystos.registries.hypotheses import spec_hash as analysis_hash
    from analystos.services.runs import start_run_request

    with session_scope() as s:
        wo = load_in_workspace(s, WorkOrder, work_order_id, workspace_id, user=user, minimum="analyst", label="work order")
        _check(wo, expected_revision)
        spec = WorkOrderSpec.model_validate(wo.spec)
        revision = wo.revision
    if not spec.executable:
        raise UnsupportedCapability(f"a {spec.spec.type} work order is a typed contract without an executor yet "
                                    "(P5 ML / P6 pipelines); it was saved but cannot start",
                                    details={"type": spec.spec.type})
    statements = list(getattr(spec.spec, "statements", []) or [])
    analyses = []
    for i, a in enumerate(spec.spec.analyses):
        raw = a.model_dump(mode="json")
        text = statements[i] if i < len(statements) else f"{a.method} of {a.asset} (work order {work_order_id})"
        analyses.append({"spec_hash": analysis_hash(raw), "spec": raw, "statement": text, "question": text})
    run, replayed = start_run_request(
        user, workspace_id, objective=spec.objective, source_ids=spec.source_ids,
        origin={"type": "work_order", "work_order_id": work_order_id, "revision": revision, "publish": "skip"},
        pins={"analyses": analyses}, idempotency=idempotency)
    if not replayed:
        with session_scope() as s:
            wo = s.get(WorkOrder, work_order_id, with_for_update=True)
            wo.run_ids = [*(wo.run_ids or []), run.id]
            wo.status = "started"
    return run, replayed
