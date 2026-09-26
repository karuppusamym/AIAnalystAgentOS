"""Pin a step to a dashboard tile or a schedule (spec v4 §7 "Pin", ADR-0021, P7-04).

A pin freezes one *verified* step version: the SQL it actually executed (after any safe correction),
or its `AnalysisSpec`, plus the semantic model and metric versions a governed query compiled from. The
frozen set is published as a `step_query` definition (the approval is its publication) and the pin
replays exactly that set — never the step's newer versions, never a re-planned query. A newer step
version shows as *upgrade available*; the owner re-pins to take it.

Both targets write outside the step (a BI-facing tile, a recurring job), so both are approvals bound to
the frozen payload's hash: the first call returns the approval request; the call with the approved
`approval_id` verifies it (`verify_for_execution`) immediately before pinning. A tile approval is single
use; a schedule re-verifies its approval at every fire, like saved analyses.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.step import PinIn
from analystos.core.errors import InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisStep, Approval, Schedule, StepPin, User
from analystos.events.bus import emit
from analystos.governance.policy import get_workspace, load_in_workspace, require_role, scoped_loader
from analystos.services import definitions
from analystos.services import steps as steps_svc

ACTIONS = {"tile": "step.pin_tile", "schedule": "step.pin_schedule"}
SCHEDULE_KIND = "step"


def _validate_definition(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    if not spec.get("step_id") or not (spec.get("sql") or spec.get("analysis_spec")):
        raise InvalidInput("a step_query definition freezes a step's executed SQL or AnalysisSpec")
    return dict(spec)


definitions.register_kind("step_query", _validate_definition)


def frozen_of(session: Session, step: AnalysisStep, version: int | None) -> tuple[dict[str, Any], Any]:
    ver = steps_svc.version_row(session, step, version)
    if step.kind not in ("query", "method") or (step.kind == "method" and not ver.spec.get("analysis_spec")):
        raise InvalidInput("only a query step or an AnalysisSpec method step can be pinned")
    view = steps_svc.view(session, step, ver)
    rec = view["verification_record"] or {}
    if ver.status != "ok" or rec.get("state") != "ACTIVE" or rec.get("verdict") != "verified":
        why = (rec.get("void") or {}).get("reason") if rec.get("state") == "VOID" else f"its status is {ver.status}"
        raise PolicyDenied(f"only a verified step version can be pinned; version {ver.version} is not ({why})")
    content = steps_svc.snapshot_content(session, ver.result_snapshot)
    executed = [r for r in ver.receipts or [] if r.get("kind") == "query" and r.get("role") in ("primary", "corrected")]
    frozen: dict[str, Any] = {"step_id": step.id, "version": ver.version, "kind": step.kind, "spec_hash": ver.spec_hash,
                              "result_hash": content.get("result_hash")}
    if step.kind == "query":
        frozen |= {"sql": executed[-1]["sql"] if executed else ver.spec.get("sql"), "source_id": ver.spec.get("source_id"),
                   "semantic_query": ver.spec.get("semantic_query"), "semantic": content.get("semantic")}
    else:
        frozen |= {"analysis_spec": ver.spec["analysis_spec"]}
    return frozen, ver


def _payload(workspace_id: str, user_id: str, body: PinIn, frozen: dict[str, Any]) -> dict[str, Any]:
    base = {"action": ACTIONS[body.target], "workspace_id": workspace_id, "owner_id": user_id, "step_id": frozen["step_id"],
            "version": frozen["version"], "frozen_hash": stable_hash(frozen)}
    if body.target == "tile":
        return base | {"dashboard": (body.dashboard or "Pinned steps")[:200], "destination": body.destination or "preview"}
    return base | {"name": body.name, "cron": body.cron, "timezone": body.timezone}


def pin(session: Session, user: User, step: AnalysisStep, body: PinIn) -> dict[str, Any]:
    """Approval first; with the approved id, the pin (and its chart artifact or schedule)."""
    from analystos.governance.approvals import consume, request_approval, verify_for_execution

    require_role(session, user, step.workspace_id, "editor")
    if body.target == "schedule":
        from analystos.services import schedules

        if not body.name or not body.cron:
            raise InvalidInput("a schedule pin needs name and cron")
        schedules.validate(SCHEDULE_KIND, body.cron, body.timezone, {})
    frozen, ver = frozen_of(session, step, body.version)
    payload = _payload(step.workspace_id, user.id, body, frozen)
    ws = get_workspace(session, step.workspace_id)
    if not body.approval_id:
        apr = request_approval(session, workspace_id=step.workspace_id, run_id=None, action=ACTIONS[body.target], payload=payload,
                               plan_hash=None, policy_version=ws.policy_version, requested_by=user.id, risk_tier="medium",
                               destination=payload.get("destination") or "schedule",
                               affected_assets=sorted({a for r in ver.receipts or [] for a in r.get("referenced_assets") or []}),
                               evidence={"step_id": step.id, "version": ver.version, "result_hash": frozen.get("result_hash")})
        return {"status": "approval_required", "approval_id": apr.id, "payload_hash": apr.payload_hash,
                "expires_at": apr.expires_at.isoformat() if apr.expires_at else None, "frozen": frozen}
    existing = session.scalar(select(StepPin).where(StepPin.workspace_id == step.workspace_id,
                                                    StepPin.approval_id == body.approval_id))
    if existing is not None:
        return {"status": "pinned", "pin": pin_view(existing)}
    apr = session.get(Approval, body.approval_id, with_for_update=True)
    if apr is None or apr.workspace_id != step.workspace_id or apr.action != ACTIONS[body.target] or apr.requested_by != user.id:
        raise PolicyDenied("the approval does not cover pinning this step")
    verify_for_execution(session, body.approval_id, payload=payload, plan_hash=None)
    publisher = apr.decided_by or user.id
    row = definitions.publish_frozen(session, step.workspace_id, "step_query", step.id.replace("_", "-"), frozen,
                                     created_by=user.id, published_by=publisher, title=f"Pinned step: {step.title}"[:300])
    ref = definitions.ref_of(row).model_dump(mode="json")
    p = StepPin(id=new_id("spin"), workspace_id=step.workspace_id, step_id=step.id, step_version=ver.version, target=body.target,
                frozen=frozen, frozen_hash=payload["frozen_hash"], definition=ref, approval_id=body.approval_id, last_result={},
                created_by=f"user:{user.id}", created_at=utcnow())
    session.add(p)
    session.flush()
    if body.target == "tile":
        from analystos.artifacts.registry import save_artifact

        consume(session, apr)
        art = save_artifact(session, workspace_id=step.workspace_id, type_="chart", name=f"pin-{p.id}", creator_user=user.id,
                            status="approved", content={"title": step.title, "sql": frozen.get("sql"), "chart": ver.chart_spec,
                                                        "dashboard": payload["dashboard"], "destination": payload["destination"],
                                                        "approval_id": body.approval_id, "frozen": frozen,
                                                        "origin": {"type": "step", "step_id": step.id, "version": ver.version,
                                                                   "pin_id": p.id}})
        p.target_id = art.id
        steps_svc._link(session, step.workspace_id, ("step", step.id), "pinned_to", ("artifact", art.id))
    else:
        from analystos.services import pins, schedules

        config = {"pin_id": p.id, "frozen_hash": p.frozen_hash, "approval_id": body.approval_id}
        sch = schedules.create_schedule(session, user, step.workspace_id, name=body.name, kind=SCHEDULE_KIND, cron=body.cron,
                                        timezone=body.timezone, config=config)
        session.flush()
        sch.pins = pins.saved_analysis_pins(ref, frozen.get("semantic"))
        pins.refresh(session, sch)
        p.target_id = sch.id
        steps_svc._link(session, step.workspace_id, ("step", step.id), "pinned_to", ("schedule", sch.id))
    emit(step.workspace_id, "step.pinned", {"pin_id": p.id, "step_id": step.id, "version": ver.version, "target": body.target,
                                            "target_id": p.target_id}, actor=f"user:{user.id}", session=session)
    return {"status": "pinned", "pin": pin_view(p)}


def pin_view(p: StepPin) -> dict[str, Any]:
    return {"id": p.id, "step_id": p.step_id, "step_version": p.step_version, "target": p.target, "target_id": p.target_id,
            "frozen": p.frozen, "frozen_hash": p.frozen_hash, "definition": p.definition, "approval_id": p.approval_id,
            "last_result": p.last_result, "created_by": p.created_by,
            "created_at": p.created_at.isoformat() if p.created_at else None}


@scoped_loader
def load_pin(session: Session, user: User, pin_id: str, workspace_id: str | None = None, minimum: str = "viewer") -> StepPin:
    p = load_in_workspace(session, StepPin, pin_id, workspace_id, user=user, minimum=minimum, label="pin")
    steps_svc.load_step(session, user, p.step_id, p.workspace_id, minimum)
    return p


def state(session: Session, p: StepPin) -> dict[str, Any]:
    """Pinned vs current: a newer step version or definition is *upgrade available*; a retired or rejected
    pinned metric blocks the replay (computed in code, ADR-0021)."""
    from analystos.services.pins import _definition_item, _semantic_items

    items = [i for i in [_definition_item(session, p.definition or {})] if i is not None]
    semantic = (p.frozen or {}).get("semantic")
    if semantic:
        from analystos.services.pins import saved_analysis_pins

        items += _semantic_items(session, p.workspace_id, saved_analysis_pins(p.definition or {}, semantic)["semantic"])
    step = session.get(AnalysisStep, p.step_id)
    newer = step is not None and step.current_version > p.step_version
    blocking = [f"{i.type} {i.id}: {i.state}" for i in items if i.state in ("retired", "rejected")]
    return {"state": "blocked" if blocking else ("upgrade_available" if newer or any(i.state == "newer" for i in items)
                                                 else "current"),
            "step_current_version": step.current_version if step is not None else None, "pinned_version": p.step_version,
            "items": [i.model_dump(mode="json") for i in items], "blocking": blocking}


def replay(user: User, workspace_id: str, pin_id: str, *, runtime: Any = None) -> dict[str, Any]:
    """Re-run the frozen query of a pin on today's data (a tile refresh, a schedule fire)."""
    with session_scope() as s:
        p = s.get(StepPin, pin_id)
        if p is None or p.workspace_id != workspace_id:
            raise NotFound("pin not found")
        st = state(s, p)
        frozen = dict(p.frozen)
    if st["state"] == "blocked":
        raise PolicyDenied("the pin is blocked by a retired or rejected pinned version: " + "; ".join(st["blocking"]))
    rt = runtime if runtime is not None else steps_svc.GatewayRuntime(user, workspace_id)
    if frozen["kind"] == "query":
        res = rt.sql(frozen["sql"], source_id=frozen.get("source_id"), step_id=frozen["step_id"])
        result = {"query_id": res.query_id, "result_hash": res.result_hash, "row_count": res.row_count,
                  "columns": list(res.columns), "rows": [list(r) for r in res.rows[:50]], "truncated": bool(res.truncated),
                  "sql": frozen["sql"]}
    else:
        res = rt.analysis(frozen["analysis_spec"], step_id=frozen["step_id"])
        result = {"query_ids": res["query_ids"], "stat": res["stat"]}
    result |= {"at": utcnow().isoformat(), "pin_state": st["state"], "pinned_version": st["pinned_version"],
               "step_current_version": st["step_current_version"],
               "changed_since_pin": result.get("result_hash") != frozen.get("result_hash") if "result_hash" in result else None}
    with session_scope() as s:
        p = s.get(StepPin, pin_id)
        p.last_result = result
        emit(workspace_id, "step.pin_refreshed", {"pin_id": pin_id, "step_id": p.step_id, "pin_state": st["state"],
                                                  "changed": result.get("changed_since_pin")},
             actor=f"user:{user.id}", session=s)
    return {"pin_id": pin_id, **result}


# ------------------------------------------------------------------------------------ schedule kind "step"
def verify_schedule(session: Session, user: User, workspace_id: str, name: str, cron: str, timezone: str,
                    config: dict[str, Any]) -> StepPin:
    from analystos.governance.approvals import verify_for_execution

    if set(config) != {"pin_id", "frozen_hash", "approval_id"}:
        raise InvalidInput("a step schedule names its pin, the frozen hash and the approval")
    p = session.get(StepPin, config["pin_id"])
    if p is None or p.workspace_id != workspace_id or p.frozen_hash != config["frozen_hash"]:
        raise InvalidInput("the pinned step no longer matches this schedule")
    body = PinIn(target="schedule", name=name, cron=cron, timezone=timezone)
    payload = _payload(workspace_id, user.id, body, p.frozen) | {"frozen_hash": p.frozen_hash}
    approval = verify_for_execution(session, config["approval_id"], payload=payload, plan_hash=None)
    if approval.action != ACTIONS["schedule"] or approval.workspace_id != workspace_id or approval.requested_by != user.id:
        raise PolicyDenied("the approval does not authorize this step schedule")
    return p


def execute_schedule(owner: User, workspace_id: str, schedule_id: str, srun_id: str, config: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as s:
        sch = s.get(Schedule, schedule_id)
        verify_schedule(s, owner, workspace_id, sch.name, sch.cron, sch.timezone, config)
    return replay(owner, workspace_id, config["pin_id"])
