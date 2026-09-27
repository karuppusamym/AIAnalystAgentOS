"""Workspace agents authored from a form (P7-19, spec v3 §3.4, ADR-0021).

A workspace owner describes a declarative agent (purpose, capabilities, knowledge scope, output,
budget, autonomy). `compile_form` turns that into the `kind: Agent` manifest the YAML path loads,
and the `agent` definition kind (`validate_definition`) checks it on every save and publish, by
whatever route it arrives (form or raw definitions API):

* the manifest validates exactly as a pack's would (`validation.validate_kinds`);
* **grants never widen**: every capability is an exact id, executable by the generic runtime,
  enabled in this workspace (as enablement computes it without this agent), not deprecated, not an
  external write; tools are only those the capabilities name, each enabled and not denied by policy;
  PII access, row limit, query and cost budgets stay within the workspace policy; no python entry,
  behaviours, requires or permissions; certification is always stored as draft.

Publishing the definition is the workspace's certification of its own composition (as for workspace
playbooks, `binding._definition_manifest`); `bind_run` re-checks the grants when a run binds the
agent, and the generic runtime still validates each action against enablement at execution time.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.capabilities import enablement, registry
from analystos.capabilities.agents import GENERIC_ENTRY, AgentBody
from analystos.capabilities.validation import EXECUTABLE_KINDS
from analystos.contracts.agent_form import (
    AGENT_KEY,
    FORM_OUTPUT_TYPES,
    MAX_LLM_CALLS,
    PII_RANK,
    AgentForm,
)
from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.definition import RUNNABLE_STATUSES
from analystos.core.errors import InvalidInput, NotFound, PolicyDenied

KIND = "agent"
TAG = "workspace_agent"
FORM_PURPOSE = "agent_actions"  # the generic runtime's proposal purpose (config/models.yaml)
_SCOPE_INPUTS = {"assets": "$scope.assets", "tables": "$scope.assets", "objective": "$objective"}


def _policy(session: Session, workspace_id: str) -> Any:
    from analystos.governance.policy import get_workspace, load_policy

    return load_policy(session, get_workspace(session, workspace_id))


def default_input(m: CapabilityManifest) -> tuple[dict[str, Any] | None, str | None]:
    """The input a default action can use (scope placeholders only), or None with the reason."""
    schema = m.input_schema or {}
    props = schema.get("properties") or {}
    out = {k: v for k, v in _SCOPE_INPUTS.items() if k in props}
    missing = [r for r in schema.get("required") or [] if r not in out]
    if missing:
        return None, f"needs {', '.join(missing)}, which only a model proposal can fill"
    return out, None


def capability_reason(m: CapabilityManifest | None, cap_id: str, enabled: dict[str, bool]) -> str | None:
    """Why this workspace cannot grant `cap_id` to an agent it authors, or None."""
    if m is None:
        return f"{cap_id} is not a registered capability"
    if m.kind not in EXECUTABLE_KINDS or m.spec.get("call") != "context" or not (m.entry or "").startswith("python:"):
        return f"{m.ref} is not executable by a declarative agent"
    if m.side_effect == "write_external":
        return f"{m.ref} writes outside the platform; that needs an approval step, not an agent grant"
    if not enabled.get(m.id, False):
        return f"{m.ref} is not enabled in this workspace"
    if why := registry.install_reason(m):
        return f"{m.ref} is unavailable on this installation: {why}"
    if m.certification.status == "deprecated":
        return f"{m.ref} is deprecated"
    return None


def grant_problems(session: Session, workspace_id: str, m: CapabilityManifest) -> list[str]:
    """Everything in the manifest that would grant more than this workspace has granted."""
    snap = registry.current()
    problems: list[str] = []
    if m.kind != "Agent":
        return ["a workspace agent definition must be a kind: Agent manifest"]
    if m.entry != GENERIC_ENTRY:
        problems.append(f"a workspace agent runs on {GENERIC_ENTRY} only (no python entry)")
    if m.side_effect == "write_external":
        problems.append("a workspace agent cannot write outside the platform")
    if m.requires or m.permissions:
        problems.append("a workspace agent cannot declare requires or permissions")
    if m.id in snap.manifests:
        problems.append(f"{m.id} is an installed capability; a workspace agent cannot replace it")
    try:
        b = AgentBody.model_validate(m.spec)
    except ValidationError:
        return problems  # the manifest validation reports the shape
    if b.behaviours:
        problems.append("a workspace agent cannot declare behaviours")
    enabled = enablement.enabled_map(session, workspace_id, snap)
    policy = _policy(session, workspace_id)
    allowed_tools: set[str] = set()
    for ref in b.capabilities:
        if any(c in ref for c in "*?["):
            problems.append(f"{ref}: name each capability exactly (no patterns)")
            continue
        cap = snap.manifests.get(ref)
        if (reason := capability_reason(cap, ref, enabled)) is not None:
            problems.append(reason)
        elif cap is not None and cap.spec.get("tool"):
            allowed_tools.add(cap.spec["tool"])
    for tool in b.tools:
        if tool not in allowed_tools:
            problems.append(f"tool {tool} is not the tool of a granted capability")
        elif not enablement.tool_enabled(session, workspace_id, tool) or tool in policy.tool_denylist:
            problems.append(f"tool {tool} is disabled or denied in this workspace")
    if b.model_purpose not in (None, FORM_PURPOSE) or [p for p in b.model_purposes if p != FORM_PURPOSE]:
        problems.append(f"a workspace agent proposes with {FORM_PURPOSE} only")
    if PII_RANK[b.policies.pii_access] > PII_RANK[policy.pii_access]:
        problems.append(f"pii_access {b.policies.pii_access} exceeds the workspace policy ({policy.pii_access})")
    if b.policies.max_rows_extract > policy.max_rows:
        problems.append(f"max rows {b.policies.max_rows_extract} exceeds the workspace policy ({policy.max_rows})")
    if b.budget.llm_calls is None or b.budget.llm_calls > MAX_LLM_CALLS:
        problems.append(f"set a model call budget of at most {MAX_LLM_CALLS}")
    if b.budget.usd is None or b.budget.usd > policy.run_cost_budget_usd:
        problems.append(f"set a cost budget of at most the workspace run budget (${policy.run_cost_budget_usd})")
    if b.budget.queries is None or b.budget.queries > policy.max_queries_per_run:
        problems.append(f"set a query budget of at most the workspace limit ({policy.max_queries_per_run})")
    if b.output_contract or b.output is None or b.output.artifact_type not in FORM_OUTPUT_TYPES:
        problems.append(f"a workspace agent writes one {'/'.join(FORM_OUTPUT_TYPES)} artifact (declare `output` only)")
    return problems


def _manifest(spec: dict[str, Any], workspace_id: str) -> CapabilityManifest:
    try:
        return CapabilityManifest.model_validate({**spec, "source": f"workspace:{workspace_id}"})
    except ValidationError as exc:
        raise InvalidInput("spec is not a capability manifest: " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from None


def validate_definition(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """The `agent` definition kind's validator: a valid Agent manifest that grants nothing new."""
    from analystos.capabilities.validation import validate_kinds

    if not AGENT_KEY.match(key):
        raise InvalidInput("an agent definition key looks like agent.<name>")
    m = _manifest(spec, workspace_id)
    if m.kind != "Agent" or m.id != key:
        raise InvalidInput(f"an agent definition's spec must be a kind: Agent manifest whose id is {key}")
    snap = registry.current()
    where = f"{m.source}: {m.id}"
    problems = [p for p in validate_kinds({**snap.manifests, m.id: m}) if p.startswith(where)]
    if problems:
        raise InvalidInput("agent does not validate: " + "; ".join(problems[:5]), details={"problems": problems})
    grants = grant_problems(session, workspace_id, m)
    if grants:
        raise PolicyDenied("the agent would exceed this workspace's grants: " + "; ".join(grants[:5]),
                           details={"problems": grants})
    tags = sorted(set(m.tags) | {TAG})
    return m.model_copy(update={"certification": m.certification.model_copy(update={"status": "draft", "evidence": None}),
                                "tags": tags}).model_dump(mode="json", exclude={"source"})


# ------------------------------------------------------------------------------------ form <-> manifest
def compile_form(session: Session, workspace_id: str, form: AgentForm, *, version: int) -> dict[str, Any]:
    """The Agent manifest for this form. Tools, determinism, cost class and certification are derived."""
    snap = registry.current()
    unknown = [c for c in form.capabilities if c not in snap.manifests]
    if unknown:
        raise InvalidInput(f"unknown capabilities: {', '.join(unknown)}")
    extra = [a for a in form.default_actions if a not in form.capabilities]
    if extra:
        raise InvalidInput(f"default actions must be among the agent's capabilities: {', '.join(extra)}")
    propose = form.autonomy.mode == "propose"
    if not propose and not form.default_actions:
        raise InvalidInput("an agent without model proposals needs at least one default action")
    actions = []
    for cid in form.default_actions:
        inputs, reason = default_input(snap.manifests[cid])
        if inputs is None:
            raise InvalidInput(f"{cid} cannot be a default action: it {reason}")
        actions.append({"capability": cid, "input": inputs})
    tools = sorted({t for c in form.capabilities if (t := snap.manifests[c].spec.get("tool"))})
    knowledge = form.knowledge.model_dump(exclude_none=True) if form.knowledge.sections else None
    return {"apiVersion": "analystos/v1", "kind": "Agent", "id": form.key, "version": f"{version}.0.0", "summary": form.title,
            "entry": GENERIC_ENTRY, "determinism": "model" if propose else "deterministic", "side_effect": "write_internal",
            "cost_class": "llm_small" if propose else "query", "certification": {"status": "draft"}, "tags": [TAG],
            "spec": {"role": form.title, "goal": form.purpose, "capabilities": list(form.capabilities), "tools": tools,
                     "model_purpose": FORM_PURPOSE if propose else None,
                     "budget": {**form.budget.model_dump(), "llm_calls": form.budget.llm_calls if propose else 0},
                     "policies": {"max_rows_extract": form.autonomy.max_rows, "pii_access": form.autonomy.pii_access,
                                  "approval_for_publish": True, "max_iterations": 1},
                     "phase": "mvp", "default_actions": actions, "knowledge": knowledge,
                     "output": {"artifact_type": form.output.type, "artifact_name": form.output.name}}}


def form_of(spec: dict[str, Any]) -> dict[str, Any] | None:
    """The form a stored manifest was built from (None when it uses fields the form cannot show)."""
    try:
        m = CapabilityManifest.model_validate(spec)
        b = AgentBody.model_validate(m.spec)
        form = AgentForm(key=m.id, title=b.role, purpose=b.goal, capabilities=b.capabilities,
                         default_actions=[a.capability for a in b.default_actions],
                         knowledge={"sections": b.knowledge.sections, "budget_chars": b.knowledge.budget_chars} if b.knowledge else {},
                         output={"type": b.output.artifact_type, "name": b.output.artifact_name} if b.output else {"name": m.summary},
                         budget={"llm_calls": b.budget.llm_calls or 0, "usd": b.budget.usd or 0, "queries": b.budget.queries or 0,
                                 "max_steps": b.budget.max_steps},
                         autonomy={"mode": "propose" if b.model_purpose else "deterministic", "pii_access": b.policies.pii_access,
                                   "max_rows": b.policies.max_rows_extract})
    except (ValidationError, TypeError, AttributeError):
        return None
    return form.model_dump(mode="json")


def options(session: Session, workspace_id: str) -> dict[str, Any]:
    """What an owner may grant here: the executable capabilities with the reason any one is not
    grantable, and the workspace ceilings for budget and data access."""
    from analystos.contracts.registry import KnowledgeSection

    snap = registry.current()
    enabled = enablement.enabled_map(session, workspace_id, snap)
    policy = _policy(session, workspace_id)
    caps = []
    for m in snap.list():
        if m.kind not in EXECUTABLE_KINDS or m.spec.get("call") != "context":
            continue
        reason = capability_reason(m, m.id, enabled)
        tool = m.spec.get("tool")
        if reason is None and tool and (not enablement.tool_enabled(session, workspace_id, tool) or tool in policy.tool_denylist):
            reason = f"tool {tool} is disabled or denied in this workspace"
        inputs, default_reason = default_input(m)
        caps.append({"id": m.id, "ref": m.ref, "summary": m.summary, "side_effect": m.side_effect, "tool": tool,
                     "certification": m.certification.status, "enabled": bool(enabled.get(m.id)), "grantable": reason is None,
                     "reason": reason, "default_input": inputs, "default_reason": default_reason})
    return {"capabilities": caps, "knowledge_sections": list(KnowledgeSection.__args__), "output_types": list(FORM_OUTPUT_TYPES),
            "limits": {"llm_calls": MAX_LLM_CALLS, "usd": policy.run_cost_budget_usd, "queries": policy.max_queries_per_run,
                       "max_rows": policy.max_rows, "max_steps": 20,
                       "pii_access": [p for p, r in PII_RANK.items() if r <= PII_RANK[policy.pii_access]]}}


def save(session: Session, user: Any, workspace_id: str, form: AgentForm, *, definition_id: str | None = None,
         expected_revision: int | None = None) -> Any:
    """Create (or edit) the draft `agent` definition for this form. Owners only (the definitions
    service enforces the role per kind)."""
    from analystos.contracts.definition import DefinitionDraftIn, DefinitionPatch
    from analystos.db.models import Definition
    from analystos.services import definitions

    if definition_id is None:
        version = definitions.next_version(session, workspace_id, KIND, form.key)
        spec = compile_form(session, workspace_id, form, version=version)
        return definitions.create_draft(session, user, workspace_id,
                                        DefinitionDraftIn(kind=KIND, key=form.key, title=form.title, spec=spec))
    row = session.get(Definition, definition_id)
    if row is None or row.workspace_id != workspace_id or row.kind != KIND:
        raise NotFound("agent definition not found")
    if row.key != form.key:
        raise InvalidInput("the key of a saved agent does not change; start a new agent instead")
    spec = compile_form(session, workspace_id, form, version=row.version)
    return definitions.update_draft(session, user, row, DefinitionPatch(title=form.title, spec=spec), expected_revision)


# ------------------------------------------------------------------------------------ run binding
def _runnable(session: Session, workspace_id: str) -> dict[str, Any]:
    """The newest runnable version of each workspace agent (in a dev workspace, the newest draft too)."""
    from analystos.db.models import Definition
    from analystos.services.definitions import EDITABLE, is_dev

    statuses = RUNNABLE_STATUSES + (EDITABLE if is_dev(session, workspace_id) else ())
    out: dict[str, Any] = {}
    for row in session.scalars(select(Definition).where(Definition.workspace_id == workspace_id, Definition.kind == KIND,
                                                        Definition.status.in_(statuses)).order_by(Definition.version.desc())):
        out.setdefault(row.key, row)
    return out


def as_manifest(row: Any) -> CapabilityManifest:
    """A workspace agent version as a run binds it: its source names the version row, and publication
    stands for certification (a draft, bound only in a dev workspace, stays draft)."""
    m = CapabilityManifest.model_validate({**row.spec, "source": f"definition:{row.id}"})
    status = "certified" if row.status in RUNNABLE_STATUSES else "draft"
    return m.model_copy(update={"certification": m.certification.model_copy(
        update={"status": status, "evidence": f"definition {row.key}@{row.version}"})})


def playbook_uses(m: CapabilityManifest) -> set[str]:
    """Every agent id a playbook's steps and expansions use."""
    steps = [s for s in m.spec.get("steps") or [] if isinstance(s, dict)]
    return {u for s in steps for u in [s.get("use")] + [e.get("use") for e in s.get("expands") or [] if isinstance(e, dict)]
            if isinstance(u, str)}


def workspace_agents(session: Session, workspace_id: str, uses: set[str] | None = None) -> dict[str, CapabilityManifest]:
    """Runnable workspace agents (optionally only those named in `uses`) that no installed capability shadows."""
    installed = registry.current().manifests
    return {key: as_manifest(row) for key, row in _runnable(session, workspace_id).items()
            if key not in installed and (uses is None or key in uses)}
