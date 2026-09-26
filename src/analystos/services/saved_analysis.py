"""Approved schedules replay one immutable Ask calculation without replanning."""
from __future__ import annotations

from sqlalchemy import select

from analystos.core.errors import InvalidInput, PolicyDenied
from analystos.core.ids import stable_hash
from analystos.db.models import Schedule
from analystos.governance.approvals import request_approval, verify_for_execution
from analystos.governance.policy import get_workspace, require_role, scoped_loader

ACTION = "ask.schedule"


def fingerprint(turn) -> str:
    return stable_hash({"turn_id": turn.id, "sql": turn.sql,
                        "semantic": (turn.provenance or {}).get("semantic")})


def payload(workspace_id, owner_id, name, cron, timezone, config):
    return {"action": ACTION, "workspace_id": workspace_id, "owner_id": owner_id, "name": name,
            "cron": cron, "timezone": timezone, "turn_id": config["turn_id"], "fingerprint": config["fingerprint"]}


def verify(session, user, workspace_id, name, cron, timezone, config):
    from analystos.services.ask import _turn_for

    if set(config) != {"turn_id", "fingerprint", "approval_id"}:
        raise InvalidInput("Saved analysis schedules require a pinned turn, fingerprint and approval.")
    turn = _turn_for(session, user, config["turn_id"], workspace_id=workspace_id)
    if turn.status != "answered" or not turn.sql or fingerprint(turn) != config["fingerprint"]:
        raise InvalidInput("The saved calculation no longer matches this schedule.")
    approval = verify_for_execution(session, config["approval_id"],
                                    payload=payload(workspace_id, user.id, name, cron, timezone, config), plan_hash=None)
    if approval.action != ACTION or approval.workspace_id != workspace_id or approval.requested_by != user.id:
        raise PolicyDenied("The approval does not authorize this saved analysis schedule.")
    return turn


@scoped_loader
def request_schedule(session, user, turn_id, *, name, cron, timezone, approval_id=None):
    from analystos.services import schedules
    from analystos.services.ask import _turn_for, turn_out

    turn = _turn_for(session, user, turn_id)
    require_role(session, user, turn.workspace_id, "editor")
    if turn.status != "answered" or not turn.sql:
        raise InvalidInput("Only an answered calculation can be scheduled.")
    if turn_out(session, turn)["evidence_status"]["state"] == "changed":
        raise InvalidInput("Refresh the changed evidence before scheduling this calculation.")
    schedules.validate("saved_analysis", cron, timezone, {})
    config = {"turn_id": turn_id, "fingerprint": fingerprint(turn), "approval_id": approval_id}
    if not approval_id:
        approval = request_approval(session, workspace_id=turn.workspace_id, run_id=None, action=ACTION,
            payload=payload(turn.workspace_id, user.id, name, cron, timezone, config), plan_hash=None,
            policy_version=get_workspace(session, turn.workspace_id).policy_version, requested_by=user.id,
            risk_tier="medium", destination="saved_analysis", affected_assets=[a["asset"] for a in turn.provenance.get("assets", [])])
        return {"status": "approval_required", "approval_id": approval.id, "expires_at": approval.expires_at.isoformat()}
    # Retrying confirmation returns the same schedule. Serialize confirmations on the approval row.
    from analystos.db.models import Approval

    session.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
    verify(session, user, turn.workspace_id, name, cron, timezone, config)
    existing = next((s for s in session.scalars(select(Schedule).where(Schedule.workspace_id == turn.workspace_id,
                    Schedule.owner_id == user.id, Schedule.kind == "saved_analysis"))
                    if s.config.get("approval_id") == approval_id), None)
    schedule = existing or schedules.create_schedule(session, user, turn.workspace_id, name=name, kind="saved_analysis",
                                                      cron=cron, timezone=timezone, config=config)
    session.flush()
    return {"status": "created", "id": schedule.id}


def changes(before, after):
    """Compare only like-shaped scalar results; grouped results retain their query receipts."""
    if before.get("result_hash") and before.get("result_hash") == after.get("result_hash"):
        return {"changed": False, "summary": "Nothing changed.", "deltas": []}
    deltas = []
    if before.get("columns") == after.get("columns") and len(before.get("rows", [])) == len(after.get("rows", [])) == 1:
        for column, old, new in zip(before["columns"], before["rows"][0], after["rows"][0], strict=True):
            if type(old) in (int, float) and type(new) in (int, float):
                deltas.append({"column": column, "before": old, "after": new, "change": new - old})
    return {"changed": True, "summary": "The result changed; review the new saved answer.", "deltas": deltas}


def execute(owner, workspace_id, schedule_id, srun_id, config):
    from analystos.db.base import session_scope
    from analystos.services.ask import rerun_turn

    with session_scope() as session:
        schedule = session.get(Schedule, schedule_id)
        old = verify(session, owner, workspace_id, schedule.name, schedule.cron, schedule.timezone, config)
        previous = dict(old.result or {})
    fresh = rerun_turn(owner, config["turn_id"])
    if fresh["status"] != "answered":
        raise InvalidInput((fresh.get("refusal") or {}).get("message", "Saved calculation was refused."))
    return {"turn_id": fresh["id"], "baseline_turn_id": config["turn_id"], "changes": changes(previous, fresh["result"])}
