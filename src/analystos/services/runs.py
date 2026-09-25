"""Run lifecycle and human-in-the-loop controls (§39, §40, §43.3)."""
from __future__ import annotations

import json
from typing import Any

from analystos.contracts.analysis import Filter
from analystos.contracts.policy import ExecutionIdentity
from analystos.core.errors import Conflict, InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Feedback, Hypothesis, Insight, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import evaluate, get_workspace, require_role, resolve_scope
from analystos.llm.router import CallContext
from analystos.runtime.context import default_router
from analystos.runtime.engine import apply_replan
from analystos.workflows.orchestrator import signal_run, start_run

TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "CANCELLED"}


def create_run(user: User, workspace_id: str, *, objective: str | None, source_ids: list[str] | None = None,
               autonomy_level: int | None = None) -> AnalysisRun:
    with session_scope() as s:
        require_role(s, user, workspace_id, "analyst")
        ws = get_workspace(s, workspace_id)
        objective = (objective or ws.objective or "").strip()
        if len(objective) < 10:
            raise InvalidInput("describe the business objective (at least 10 characters)")
        scope = resolve_scope(s, s.merge(user), workspace_id, source_ids=source_ids)
        if not scope.assets:
            raise InvalidInput("no selected, ready assets: add a source, discover it and select tables first")
        if len(set(scope.asset_sources.values())) > 1 and not source_ids:
            raise InvalidInput("the MVP analyses one source per run: pass source_ids with a single source")
        level = min(autonomy_level if autonomy_level is not None else ws.autonomy_level, ws.autonomy_level)
        identity = ExecutionIdentity(user_id=user.id, workspace_id=workspace_id, purpose="analysis")
        decision = evaluate(s, s.merge(user), identity, "run_analysis", autonomy_level=level)
        if decision.decision == "deny":
            raise PolicyDenied("analysis run denied: " + ", ".join(decision.reasons))
        run = AnalysisRun(id=new_id("run"), workspace_id=workspace_id, objective=objective, status="NEW", autonomy_level=level,
                          policy_version=ws.policy_version, requested_by=user.id,
                          scope={**scope.model_dump(), "hash": scope.scope_hash()}, instructions=[], constraints={})
        s.add(run)
        s.flush()
        emit(workspace_id, "run.created", {"objective": objective, "autonomy_level": level, "assets": scope.assets,
                                           "policy": decision.model_dump()}, run_id=run.id, actor=f"user:{user.id}", session=s)
        run_id = run.id
    wf = start_run(run_id)
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        run.workflow_id = wf
        s.expunge(run)
    return run


def get_run_for(session, user: User, run_id: str, minimum: str = "viewer") -> AnalysisRun:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    require_role(session, user, run.workspace_id, minimum)
    return run


def control(user: User, run_id: str, action: str) -> AnalysisRun:
    with session_scope() as s:
        run = get_run_for(s, user, run_id, "analyst")
        if run.status in TERMINAL:
            raise Conflict(f"run is {run.status}")
        if action == "pause":
            run.control = "pause"
        elif action == "resume":
            run.control = "run"
            if run.status == "PAUSED":
                run.status = "RUNNING"
        elif action == "cancel":
            run.control = "cancel"
        else:
            raise InvalidInput("action must be pause|resume|cancel")
        emit(run.workspace_id, "run.status", {"control": run.control, "requested_by": user.id}, run_id=run.id, session=s)
        audit(f"user:{user.id}", f"run.{action}", workspace_id=run.workspace_id, run_id=run.id, session=s)
        s.expunge(run)
    signal_run(run_id)
    return run


FEEDBACK_KINDS = {"redirect": "Narrow or change what the analysis should focus on (filters, scope, exclusions)",
                  "add_context": "Adds business context or definitions without changing scope",
                  "reject_finding": "Says a finding is wrong or not useful",
                  "deeper_analysis": "Asks for deeper analysis or a new question to investigate",
                  "question": "Asks a question about the results"}


def _interpret_redirect(run: AnalysisRun, text: str) -> dict[str, Any]:
    """LLM -> structured filters, validated against the run scope. Invalid filters are dropped."""
    router = default_router()
    ctx = CallContext(workspace_id=run.workspace_id, run_id=run.id, agent_id="supervisor", prompt_version="feedback_interpretation.v1")
    catalog = [{"asset": a, "columns": [c for c in cols if f"{a}.{c}" not in set(run.scope.get("denied_columns") or [])]}
               for a, cols in (run.scope.get("columns") or {}).items()]
    out: dict[str, Any] = {"filters": [], "focus": [text], "summary": text, "interpreted_by": "none"}
    if not router.available("feedback_interpretation", ctx):
        return out
    from analystos.agents.prompts import prompt

    try:
        resp = router.complete_json("feedback_interpretation", prompt("feedback_interpretation.v1"),
                                    json.dumps({"instruction": text, "catalog": catalog, "objective": run.objective}), ctx=ctx)
    except Exception:
        return out
    data = resp.data if isinstance(resp.data, dict) else {}
    valid = []
    for f in data.get("filters") or []:
        try:
            flt = Filter.model_validate({**f, "origin": "user_redirect"})
        except Exception:
            continue
        asset = f.get("asset") or next(iter(run.scope.get("assets") or []), None)
        cols = (run.scope.get("columns") or {}).get(asset, [])
        if flt.column in cols and f"{asset}.{flt.column}" not in set(run.scope.get("denied_columns") or []):
            valid.append({"asset": asset, "column": flt.column, "op": flt.op, "value": flt.value})
    return {"filters": valid, "focus": [str(x) for x in data.get("focus") or []] or [text],
            "summary": data.get("summary") or text, "interpreted_by": resp.model}


def submit_feedback(user: User, run_id: str, *, text: str, kind: str | None = None, target_type: str | None = None,
                    target_id: str | None = None) -> dict[str, Any]:
    from analystos.llm.jev import JevDecisions

    with session_scope() as s:
        run = get_run_for(s, user, run_id, "analyst")
        s.expunge(run)
    jev = JevDecisions(default_router())
    ctx = CallContext(workspace_id=run.workspace_id, run_id=run.id, agent_id="supervisor")
    classified_by = "user"
    if kind is None:
        v = jev.choose("feedback_classification", {"feedback": text, "objective": run.objective},
                       "What kind of feedback is `feedback` about an analysis of `objective`?", FEEDBACK_KINDS, ctx=ctx)
        kind, classified_by = (v.value, f"jev:{v.model}") if v else ("redirect", "default")
    if kind not in FEEDBACK_KINDS:
        raise InvalidInput(f"kind must be one of {sorted(FEEDBACK_KINDS)}")
    risk = jev.consequential(text, ctx=ctx)
    result: dict[str, Any] = {"kind": kind, "classified_by": classified_by,
                              "consequential_p": risk.value if risk else None}
    interpretation = _interpret_redirect(run, text) if kind in ("redirect", "deeper_analysis") else None
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id, with_for_update=True)
        fb = Feedback(id=new_id("fb"), workspace_id=run.workspace_id, run_id=run.id, user_id=user.id, kind=kind, text=text,
                      target_type=target_type, target_id=target_id, data={"interpretation": interpretation, **result})
        s.add(fb)
        run.instructions = [*run.instructions, {"kind": kind, "text": text, "at": utcnow().isoformat(), "feedback_id": fb.id}]
        if risk and risk.value >= 0.5:
            result["note"] = ("This instruction asks for an action with side effects; agents never execute it directly — "
                              "publication, scheduling and sending always go through an approval.")
        if kind in ("redirect", "deeper_analysis"):
            constraints = dict(run.constraints or {})
            constraints["filters"] = [*constraints.get("filters", []), *interpretation["filters"]]
            constraints["focus"] = [*constraints.get("focus", []), *interpretation["focus"]]
            run.constraints = constraints
            if run.status in TERMINAL - {"COMPLETED"}:
                raise Conflict(f"run is {run.status}")
            result["replan"] = apply_replan(s, run, f"user {kind}: {interpretation['summary'][:200]}", full=True)
            result["interpretation"] = interpretation
            fb.applied = True
        elif kind == "reject_finding":
            if target_type != "insight" or not target_id:
                raise InvalidInput("reject_finding needs target_type='insight' and target_id")
            ins = s.get(Insight, target_id)
            if ins is None or ins.run_id != run.id:
                raise NotFound("insight not found in this run")
            ins.status = "rejected"
            h = s.get(Hypothesis, ins.hypothesis_id)
            if h:
                h.status, h.conclusion = "rejected", (h.conclusion or "") + " — rejected by user feedback"
            result["replan"] = apply_replan(s, run, f"finding {ins.code} rejected by user", full=False)
            fb.applied = True
        elif kind == "add_context":
            from analystos.context.service import add_entry

            add_entry(s, workspace_id=run.workspace_id, kind="note", name=f"User context ({user.email})", body=text, origin="user")
            fb.applied = True
        emit(run.workspace_id, "feedback.received", {"kind": kind, "text": text[:300], **{k: v for k, v in result.items() if k != "replan"}},
             run_id=run.id, actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "feedback.submitted", workspace_id=run.workspace_id, run_id=run.id, details=result, session=s)
        result["feedback_id"] = fb.id
        if run.status == "COMPLETED" and result.get("replan"):
            run.status = "RUNNING"
    if result.get("replan"):
        with session_scope() as s:
            restart = s.get(AnalysisRun, run_id).workflow_id is None
        signal_run(run_id)
        if restart:
            start_run(run_id)
    return result


def restart_if_finished(run_id: str) -> None:
    """A replanned COMPLETED run needs a fresh orchestration loop (the old workflow has returned)."""
    start_run(run_id)
