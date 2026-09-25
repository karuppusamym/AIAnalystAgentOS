"""Generic propose -> validate -> execute runtime for declarative agents (spec v3 §3.4, P4-X03).

An agent defined only by a manifest runs here. One loop, capped by the manifest's budget:

1. **Propose.** The model (purpose `model_purpose`, routed by llm/router.py through `llm_json`, so the
   admin mode, the workspace model policy and the agent's llm_calls/usd budget apply) proposes typed
   actions chosen from the agent's bound capabilities. In `off`/`auto` mode, or when no model is
   available, the manifest's `default_actions` are the deterministic path (`model_gate` records the
   avoided call).
2. **Validate.** Each action, before anything runs: a capability the agent binds (the run's bound
   version); executable here (a python `call: context` entry); no external side effect; enabled for
   the workspace and certified when the run is autonomous; input valid against its JSON Schema;
   inside the authorized scope; within the query budget. Then the tool gate (`ToolRuntime.invoke`:
   workspace policy, role, the agent's tool binding) is passed as the action executes.
3. **Execute** through the capability's entry with the step's RunContext (governed SQL only via
   `ctx.run_sql()`), and check cancel/replan after each action.
4. **Summarise** each result back into the next proposal's history.

A rejected action is recorded with its reason and never runs. The model never executes anything,
and a model-written summary is kept only when every number in it appears in the results.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from analystos.agents.common import compact_json, llm_json, model_gate
from analystos.capabilities import enablement, registry
from analystos.capabilities.agents import AgentBody, body
from analystos.capabilities.binding import bound_manifest
from analystos.capabilities.validation import EXECUTABLE_KINDS
from analystos.contracts.capability import CapabilityManifest
from analystos.core.errors import AnalystOSError, ApprovalRequired, BudgetExceeded, PolicyDenied, RunCancelled
from analystos.db.base import session_scope
from analystos.runtime.context import RunContext

MAX_ACTIONS_PER_ROUND = 5
SUMMARY_CHARS = 1500
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
_SCOPE_KEYS = ("asset", "assets", "table", "tables")


class ActionRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Proposal:
    actions: list[Any]
    done: bool = True
    summary: str | None = None


@dataclass
class Session:
    """Everything one agent step decided, in order."""

    records: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)


# ------------------------------------------------------------------------------------ binding
def allowed_capabilities(ctx: RunContext, manifest: CapabilityManifest) -> dict[str, CapabilityManifest]:
    """The agent's capabilities at the versions this run bound. A run planned before bindings existed
    (or an agent run outside a playbook) uses the current registry's."""
    import fnmatch

    snap = registry.current()
    bound_ids = set(((ctx.run.capabilities or {}).get("manifests") or {}) if ctx.run is not None else ())
    candidates = bound_ids or set(snap.manifests)
    out: dict[str, CapabilityManifest] = {}
    for ref in body(manifest).capabilities:
        for cid in sorted(candidates):
            if fnmatch.fnmatchcase(cid, ref) and cid not in out:
                m = bound_manifest(ctx.run, cid) if cid in bound_ids else snap.manifests.get(cid)
                if m is not None:
                    out[cid] = m
    return out


def instantiate(value: Any, ctx: RunContext) -> Any:
    """Fill `$scope.assets` / `$objective` in a default action's input."""
    if value == "$scope.assets":
        return list(ctx.scope.assets)
    if value == "$objective":
        return ctx.run.objective
    if isinstance(value, dict):
        return {k: instantiate(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [instantiate(v, ctx) for v in value]
    return value


# ------------------------------------------------------------------------------------ validation
def _scope_problem(ctx: RunContext, inputs: dict[str, Any]) -> str | None:
    allowed = set(ctx.scope.assets)
    for key in _SCOPE_KEYS:
        value = inputs.get(key)
        names = value if isinstance(value, list) else [value] if value is not None else []
        outside = [str(n) for n in names if n not in allowed]
        if outside:
            return f"{', '.join(outside[:5])} outside the authorized scope of this run"
    return None


def validate_action(ctx: RunContext, agent: CapabilityManifest, allowed: dict[str, CapabilityManifest], action: Any,
                    explicit: dict[str, bool]) -> tuple[CapabilityManifest, dict[str, Any]]:
    """The capability and input of a valid action; raises ActionRejected with the reason otherwise."""
    from jsonschema import Draft202012Validator

    if not isinstance(action, dict) or not isinstance(action.get("capability"), str):
        raise ActionRejected("malformed action: it needs `capability` (a string) and `input` (an object)")
    cid, inputs = action["capability"], action.get("input", {})
    if not isinstance(inputs, dict):
        raise ActionRejected(f"malformed action: the input of {cid} must be an object")
    m = allowed.get(cid)
    if m is None:
        known = cid in registry.current().manifests or bound_manifest(ctx.run, cid) is not None
        raise ActionRejected(f"{cid} is not bound to {agent.id}" if known else f"{cid} is not a registered capability")
    if m.kind not in EXECUTABLE_KINDS or m.spec.get("call") != "context" or not (m.entry or "").startswith("python:"):
        raise ActionRejected(f"{m.ref} is not executable by the generic runtime (needs a python entry with spec.call: context)")
    if m.side_effect == "write_external":
        raise ActionRejected(f"{m.ref} writes outside the platform; that needs an approval step in a playbook, not an agent action")
    problem = enablement.usable(m, registry.current(), explicit, autonomous_run=enablement.autonomous(ctx.run))
    if problem:
        raise ActionRejected(problem)
    errors = sorted(Draft202012Validator(m.input_schema or {"type": "object"}).iter_errors(inputs), key=lambda e: list(e.path))
    if errors:
        where = "/".join(map(str, errors[0].path)) or "input"
        raise ActionRejected(f"input does not match {m.id} input_schema at {where}: {errors[0].message}")
    problem = _scope_problem(ctx, inputs)
    if problem:
        raise ActionRejected(problem)
    if m.side_effect == "read_source" and not ctx.budget_left("queries"):
        raise ActionRejected(f"{agent.id} query budget ({ctx.budget.queries}) exhausted for this step")
    return m, inputs


# ------------------------------------------------------------------------------------ execution
def _summary(value: Any) -> str:
    from analystos.tools.registry import _summarize

    return compact_json(_summarize(value))[:SUMMARY_CHARS]


def _record(ctx: RunContext, state: Session, round_no: int, action: Any, status: str, reason: str | None = None,
            result: Any = None) -> None:
    cid = action.get("capability") if isinstance(action, dict) else None
    rec = {"round": round_no, "capability": cid, "input": action.get("input") if isinstance(action, dict) else None,
           "status": status, "reason": reason, "summary": _summary(result) if result is not None else None}
    state.records.append(rec)
    ctx.event("agent.action", {k: rec[k] for k in ("round", "capability", "status", "reason")})
    if status != "executed":
        ctx.say(f"Action {cid or '?'} {status}: {reason}", kind="decision", data={"input": rec["input"]})


def execute(ctx: RunContext, agent: CapabilityManifest, allowed: dict[str, CapabilityManifest], actions: list[Any],
            state: Session, round_no: int, explicit: dict[str, bool]) -> None:
    for action in actions[:MAX_ACTIONS_PER_ROUND]:
        try:
            m, inputs = validate_action(ctx, agent, allowed, action, explicit)
        except ActionRejected as exc:
            _record(ctx, state, round_no, action, "rejected", exc.reason)
            continue
        fn = registry.resolve_python(m.entry)
        tool = m.spec.get("tool")
        try:
            call = lambda fn=fn, inputs=inputs: fn(ctx, **inputs)  # noqa: E731
            result = ctx.tools().invoke(tool, {"capability": m.ref, **inputs}, call) if tool else call()
        except RunCancelled:
            raise
        except (PolicyDenied, ApprovalRequired, BudgetExceeded) as exc:
            _record(ctx, state, round_no, action, "rejected", exc.message)
            continue
        except AnalystOSError as exc:
            _record(ctx, state, round_no, action, "failed", f"{exc.code}: {exc.message}")
            continue
        except Exception as exc:  # a broken capability fails its action, not the agent step
            _record(ctx, state, round_no, action, "failed", f"{type(exc).__name__}: {exc}"[:300])
            continue
        _record(ctx, state, round_no, action, "executed", result=result)
        state.results.append({"capability": m.ref, "input": inputs, "result": result})
        ctx.check_control()


def propose(ctx: RunContext, agent: CapabilityManifest, spec: AgentBody, allowed: dict[str, CapabilityManifest],
            state: Session, remaining: int) -> Proposal | None:
    payload = {"role": spec.role, "goal": spec.goal, "objective": ctx.run.objective,
               "scope": {"assets": list(ctx.scope.assets)},
               "capabilities": [{"id": m.id, "summary": m.summary, "input_schema": m.input_schema} for m in allowed.values()],
               "history": [{k: r[k] for k in ("capability", "input", "status", "reason", "summary")} for r in state.records],
               "remaining_rounds": remaining}
    data, _ = llm_json(ctx, spec.model_purpose, "agent_actions.v1", payload)
    if not isinstance(data, dict):
        return None
    actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    summary = data.get("summary") if isinstance(data.get("summary"), str) else None
    return Proposal(actions=actions, done=bool(data.get("done")) or not actions, summary=summary)


def _guarded(summary: str | None, results: list[dict[str, Any]]) -> bool:
    """A model summary may only use numbers that appear in the results (or small counts)."""
    if not summary:
        return False
    allowed = set(_NUM.findall(compact_json(results))) | {str(i) for i in range(0, 11)}
    return set(_NUM.findall(summary)) <= allowed


def _template(agent: CapabilityManifest, state: Session) -> str:
    done = [r for r in state.records if r["status"] == "executed"]
    lines = [f"- **{r['capability']}**: {(r['summary'] or '')[:300]}" for r in done]
    refused = len(state.records) - len(done)
    return "\n".join(lines or ["- No action was executed."]) + (f"\n- {refused} action(s) rejected or failed." if refused else "")


def run_agent(ctx: RunContext, agent: CapabilityManifest) -> dict[str, Any]:
    spec = body(agent)
    allowed = allowed_capabilities(ctx, agent)
    with session_scope() as s:
        explicit = enablement.overrides(s, ctx.workspace.id)
    state = Session()
    deterministic = [a.model_dump() for a in spec.default_actions]
    deterministic = [{**a, "input": instantiate(a["input"], ctx)} for a in deterministic]
    use_model = bool(spec.model_purpose) and model_gate(
        ctx, spec.model_purpose, {"goal": spec.goal, "capabilities": sorted(allowed)}, deterministic_ok=bool(deterministic))
    mode, model_summary = "deterministic", None
    if use_model:
        for round_no in range(1, spec.budget.max_steps + 1):
            proposal = propose(ctx, agent, spec, allowed, state, spec.budget.max_steps - round_no)
            if proposal is None:
                break
            mode = "model"
            execute(ctx, agent, allowed, proposal.actions, state, round_no, explicit)
            model_summary = proposal.summary or model_summary
            if proposal.done:
                break
    if mode == "deterministic" and deterministic:
        execute(ctx, agent, allowed, deterministic, state, 1, explicit)
    summary, source = (model_summary, "model") if _guarded(model_summary, state.results) else (_template(agent, state), "template")
    ctx.say(f"{spec.role}: {len([r for r in state.records if r['status'] == 'executed'])} action(s) executed, "
            f"{len([r for r in state.records if r['status'] != 'executed'])} rejected or failed ({mode}).", kind="decision")
    artifact_id = None
    if spec.output is not None:
        from analystos.artifacts.registry import link, save_artifact

        with session_scope() as s:
            art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_=spec.output.artifact_type,
                                name=spec.output.artifact_name, creator_agent=ctx.agent.id,
                                content={"agent": agent.ref, "mode": mode, "summary_markdown": summary, "summary_source": source,
                                         "results": state.results, "actions": state.records})
            link(s, ctx.workspace.id, ("run", ctx.run.id), "produced", ("artifact", art.id), run_id=ctx.run.id)
            artifact_id = art.id
    return {"agent": agent.ref, "mode": mode, "actions": state.records, "results": state.results, "summary": summary,
            "summary_source": source, "usage": dict(ctx.usage), "artifact_id": artifact_id}
