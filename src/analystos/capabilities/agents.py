"""Agent manifests (spec v3 §3.4, P4-X03): the `spec` body of a `kind: Agent` capability.

Every field here is enforced somewhere; unknown fields fail the load (`extra="forbid"`), so a
manifest cannot carry decorative settings that look governed but are not:

  behaviours      named python entries a playbook step can select; the manifest `entry` is the
                  default. `builtin:generic` runs the propose -> validate -> execute runtime.
  capabilities    what the agent may use: validated at load (unknown ids fail), bound into the plan
                  hash, and the only actions the generic runtime accepts.
  tools           the tool gate binding (`ToolRuntime.authorize`, bound=True).
  model_purpose / model_purposes
                  the only purposes `agents.common.llm_json` routes for this agent (others degrade to
                  the deterministic path); the generic runtime proposes with `model_purpose`.
  budget          per task execution: model calls, model USD, source queries and generic steps.
  policies        tighten the workspace policy for this agent's steps (runtime/context.py).
  default_actions the deterministic path of a generic agent (`off`/`auto` modes run it without a model).
  output          where a generic agent's results are persisted.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.registry import AgentPolicies, AgentSpec

GENERIC_ENTRY = "builtin:generic"


class AgentBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_calls: int | None = Field(None, ge=0)  # model proposals / chat calls per task execution; None = unbounded
    usd: float | None = Field(None, ge=0)  # model cost per task execution
    queries: int | None = Field(None, ge=0)  # source statements (read_source actions) per task execution
    max_steps: int = Field(4, ge=1, le=20)  # propose/execute rounds of the generic runtime


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


def to_agent_spec(m: CapabilityManifest) -> AgentSpec:
    """The AgentSpec the tool gate and agent_definition rows use, derived from the manifest."""
    b = body(m)
    return AgentSpec(id=short_id(m.id), version=m.version, name=b.role, description=b.goal, capabilities=list(m.tags),
                     skills=[c.split(".", 1)[1] for c in b.capabilities if c.startswith("skill.")], tools=list(b.tools),
                     model_profile=b.purposes[0] if b.purposes else "none", prompt_version=b.prompt_version,
                     policies=b.policies, phase=b.phase)


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

