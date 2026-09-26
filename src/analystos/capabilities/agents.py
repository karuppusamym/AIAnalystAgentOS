"""Agent manifests (spec v3 §3.4, P4-X03, FND-006): the `spec` body of a `kind: Agent` capability.

The manifest is the agent contract's only source. `to_agent_spec` derives the `AgentSpec` view that
the runtime, the tool gate, the `agent_definition` rows and `GET /api/agents` use, so the two cannot
drift. Unknown fields fail the load (`extra="forbid"`). Each field is enforced or, where marked,
informational (docs/20-contracts/03-agent-contract.md):

  role / goal     the generic runtime's prompt; display name/description elsewhere (informational)
  behaviours      named python entries a playbook step can select; the manifest `entry` is the
                  default. `builtin:generic` runs the propose -> validate -> execute runtime.
  capabilities    what the agent may use: validated at load (unknown ids fail), bound into the plan
                  hash, and the only actions the generic runtime accepts.
  tools           the tool gate binding (`ToolRuntime.authorize`, bound=True).
  model_purpose / model_purposes
                  the only purposes `agents.common.llm_json` routes for this agent (others degrade to
                  the deterministic path); the generic runtime proposes with `model_purpose`.
  prompt_version  recorded on every model call the agent makes (provenance; informational).
  budget          per task execution: model calls, model USD, source queries and generic steps.
  policies        tighten the workspace policy for this agent's steps (runtime/context.py).
  phase           seeds the platform-wide enabled flag (non-mvp agents start disabled; informational after).
  default_actions the deterministic path of a generic agent (`off`/`auto` modes run it without a model).
  knowledge       the knowledge sections (and budget) the context compiler may put in this agent's
                  prompts (agents/common.compile_for); none declared = no knowledge sections.
  output_contract the artifact and record types the agent may persist, each with the schema its
                  content must satisfy (`enforce_output`, just before the write; OutputContractViolation).
  output          where a generic agent's results are persisted; implies an `output_contract` entry.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.registry import (
    OUTPUT_RECORD_TYPES,
    AgentBudget,
    AgentKnowledge,
    AgentOutputDecl,
    AgentPolicies,
    AgentSpec,
)
from analystos.core.errors import OutputContractViolation

GENERIC_ENTRY = "builtin:generic"

__all__ = ["GENERIC_ENTRY", "GENERIC_OUTPUT_SCHEMA", "ActionTemplate", "AgentBody", "AgentBudget", "AgentKnowledge",
           "AgentOutput", "AgentOutputDecl", "body", "contract_for", "declaration_problems", "declared_outputs",
           "enforce_output", "entry_for", "from_legacy", "is_generic", "output_types", "schema_problem", "short_id",
           "to_agent_spec"]


class ActionTemplate(BaseModel):
    """One deterministic action. String values `$scope.assets` / `$objective` are filled at run time."""

    model_config = ConfigDict(extra="forbid")

    capability: str
    input: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_type: str = "agent_output"
    artifact_name: str


class AgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    goal: str
    behaviours: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    model_purpose: str | None = None
    model_purposes: list[str] = Field(default_factory=list)
    prompt_version: str = "v1"
    budget: AgentBudget = Field(default_factory=AgentBudget)
    policies: AgentPolicies = Field(default_factory=AgentPolicies)
    phase: str = "mvp"  # mvp agents are enabled in agent_definition at seed; others are registered disabled
    default_actions: list[ActionTemplate] = Field(default_factory=list)
    knowledge: AgentKnowledge | None = None
    output_contract: list[AgentOutputDecl] = Field(default_factory=list)
    output: AgentOutput | None = None

    @field_validator("behaviours")
    @classmethod
    def _python(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k, e in v.items() if not (e.startswith("python:") or e == GENERIC_ENTRY)]
        if bad:
            raise ValueError(f"behaviours {bad} must be python:<module>:<attr> or {GENERIC_ENTRY}")
        return v

    @field_validator("policies")
    @classmethod
    def _publish_gate(cls, v: AgentPolicies) -> AgentPolicies:
        if not v.approval_for_publish:
            raise ValueError("policies.approval_for_publish cannot be disabled: publication always needs an approval")
        return v

    @field_validator("output_contract")
    @classmethod
    def _unique_outputs(cls, v: list[AgentOutputDecl]) -> list[AgentOutputDecl]:
        types = [d.type for d in v]
        dup = sorted({t for t in types if types.count(t) > 1})
        if dup:
            raise ValueError(f"output_contract declares {dup} more than once")
        return v

    @property
    def purposes(self) -> list[str]:
        return ([self.model_purpose] if self.model_purpose else []) + [p for p in self.model_purposes if p != self.model_purpose]


def body(m: CapabilityManifest) -> AgentBody:
    return AgentBody.model_validate(m.spec)


def short_id(agent_cap_id: str) -> str:
    """`agent.investigator` -> `investigator` (the id of agent_definition rows and run tasks)."""
    return agent_cap_id.split(".", 1)[1]


def is_generic(m: CapabilityManifest, behaviour: str | None = None) -> bool:
    return entry_for(m, behaviour) == GENERIC_ENTRY


def entry_for(m: CapabilityManifest, behaviour: str | None = None) -> str | None:
    """The entry a step runs: a named behaviour, else the manifest's default entry."""
    if behaviour:
        b = body(m).behaviours
        if behaviour not in b:
            raise KeyError(f"{m.id} has no behaviour {behaviour!r}")
        return b[behaviour]
    return m.entry


# The content a generic agent persists (agents/generic.run_agent); its `output` declares this schema.
GENERIC_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["agent", "mode", "summary_markdown", "summary_source", "results", "actions"],
    "properties": {"agent": {"type": "string"}, "mode": {"enum": ["model", "deterministic"]},
                   "summary_markdown": {"type": "string"}, "summary_source": {"enum": ["model", "template"]},
                   "results": {"type": "array"}, "actions": {"type": "array"}}}


def declared_outputs(b: AgentBody) -> list[AgentOutputDecl]:
    """`output_contract`, plus the generic runtime's `output` when the contract does not list its type."""
    out = list(b.output_contract)
    if b.output is not None and all(d.type != b.output.artifact_type for d in out):
        out.append(AgentOutputDecl(type=b.output.artifact_type, schema=GENERIC_OUTPUT_SCHEMA))
    return out


def to_agent_spec(m: CapabilityManifest) -> AgentSpec:
    """The AgentSpec view of a manifest: the only way an AgentSpec for a registered agent is made."""
    b = body(m)
    return AgentSpec(id=short_id(m.id), capability_id=m.id, version=m.version, name=b.role, description=b.goal,
                     capabilities=list(m.tags), skills=[c.split(".", 1)[1] for c in b.capabilities if c.startswith("skill.")],
                     tools=list(b.tools), model_profile=b.purposes[0] if b.purposes else "none", model_purposes=b.purposes,
                     prompt_version=b.prompt_version, policies=b.policies, budget=b.budget, knowledge=b.knowledge,
                     output_contract=declared_outputs(b), entry=m.entry, behaviours=dict(b.behaviours),
                     certification=m.certification.status, source=m.source, phase=b.phase)


def contract_for(run: Any, agent_id: str) -> AgentSpec | None:
    """The contract of `agent_id` as this run bound it, else as the current registry defines it."""
    from analystos.capabilities import registry
    from analystos.capabilities.binding import bound_manifest

    cap_id = f"agent.{agent_id}"
    m = (bound_manifest(run, cap_id) if run is not None else None) or registry.current().manifests.get(cap_id)
    return to_agent_spec(m) if m is not None and m.kind == "Agent" else None


# ------------------------------------------------------------------------------------ output contract
def output_types() -> set[str]:
    from analystos.artifacts.registry import ARTIFACT_TYPES

    return set(ARTIFACT_TYPES) | set(OUTPUT_RECORD_TYPES)


@lru_cache(maxsize=64)
def _contract_model(ref: str) -> Any:
    """`contract:bi.ChartSpec` -> the Pydantic model `analystos.contracts.bi.ChartSpec`."""
    import importlib

    module, _, name = ref[len("contract:"):].rpartition(".")
    if not module or not name:
        raise ValueError(f"{ref}: expected contract:<module>.<Model>")
    model = getattr(importlib.import_module(f"analystos.contracts.{module}"), name, None)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise ValueError(f"{ref}: analystos.contracts.{module} has no Pydantic model {name}")
    return model


def schema_problem(schema: str | dict[str, Any] | None, content: Any) -> str | None:
    """Why `content` does not satisfy a declared output schema, or None."""
    if schema is None:
        return None if isinstance(content, dict) else f"content: must be a JSON object, got {type(content).__name__}"
    if isinstance(schema, str):
        from pydantic import ValidationError

        try:
            _contract_model(schema).model_validate(content)
        except ValidationError as exc:
            e = exc.errors()[0]
            return f"{'.'.join(map(str, e['loc'])) or 'content'}: {e['msg']}"
        return None
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(schema).iter_errors(content), key=lambda e: list(e.path))
    if errors:
        return f"{'/'.join(map(str, errors[0].path)) or 'content'}: {errors[0].message}"
    return None


def declaration_problems(decl: AgentOutputDecl) -> list[str]:
    """Load-time checks of one output declaration (capabilities/validation.py)."""
    problems = []
    if decl.type not in output_types():
        problems.append(f"output_contract: unknown output type {decl.type}")
    if isinstance(decl.schema_, str):
        if not decl.schema_.startswith("contract:"):
            problems.append(f"output_contract {decl.type}: schema must be contract:<module>.<Model> or a JSON Schema object")
        else:
            try:
                _contract_model(decl.schema_)
            except (ValueError, ImportError) as exc:
                problems.append(f"output_contract {decl.type}: {exc}")
    elif isinstance(decl.schema_, dict):
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        try:
            Draft202012Validator.check_schema(decl.schema_)
        except SchemaError as exc:
            problems.append(f"output_contract {decl.type}: not a valid JSON Schema: {exc.message}")
    return problems


def enforce_output(agent: AgentSpec | None, agent_id: str, type_: str, content: Any) -> None:
    """Refuse (OutputContractViolation) an output the agent's contract does not declare, or whose
    content fails the declared schema. Called immediately before the write, so nothing is persisted."""
    if agent is None:
        raise OutputContractViolation(f"agent {agent_id} has no manifest, so it declares no outputs: {type_} refused",
                                      details={"agent": agent_id, "output": type_, "reason": "no_manifest"})
    declared = sorted(d.type for d in agent.output_contract)
    decl = agent.output(type_)
    if decl is None:
        raise OutputContractViolation(f"agent {agent.id} does not declare output {type_} (declared: {declared})",
                                      details={"agent": agent.id, "output": type_, "reason": "undeclared", "declared": declared})
    problem = schema_problem(decl.schema_, content)
    if problem:
        raise OutputContractViolation(f"agent {agent.id} output {type_} does not match its declared schema at {problem}",
                                      details={"agent": agent.id, "output": type_, "reason": "schema", "problem": problem})


# ------------------------------------------------------------------------------------ legacy files
def from_legacy(data: dict[str, Any]) -> dict[str, Any]:
    """Old `agent:` YAML (compatibility window) -> manifest dict. Free-text skill names that do not
    exist fail the load like any other unknown reference, so migrate the file instead of relying on this."""
    a = dict(data)
    phase = a.get("phase", "mvp")
    return {"apiVersion": "analystos/v1", "kind": "Agent", "id": f"agent.{a['id']}", "version": _semver(a.get("version", "1.0")),
            "summary": a.get("name", a["id"]), "entry": None, "determinism": "model", "side_effect": "write_internal",
            "cost_class": "llm_small", "certification": {"status": "certified" if phase == "mvp" else "draft"},
            "tags": list(a.get("capabilities") or []),
            "spec": {"role": a.get("name", a["id"]), "goal": a.get("description", ""),
                     "capabilities": [f"skill.{s}" for s in a.get("skills") or []], "tools": list(a.get("tools") or []),
                     "prompt_version": a.get("prompt_version", "v1"), "policies": a.get("policies") or {}, "phase": phase}}


def _semver(v: Any) -> str:
    parts = str(v).split(".")
    return ".".join((parts + ["0", "0"])[:3])
