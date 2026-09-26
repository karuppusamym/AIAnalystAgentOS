"""Agent, skill and tool contracts (§12, §14, §15)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

Risk = Literal["low", "medium", "high"]


class AgentPolicies(BaseModel):
    max_rows_extract: int = 500_000
    pii_access: Literal["none", "restricted", "allowed"] = "restricted"
    approval_for_publish: bool = True
    max_iterations: int = 3


class AgentBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_calls: int | None = Field(None, ge=0)  # model proposals / chat calls per task execution; None = unbounded
    usd: float | None = Field(None, ge=0)  # model cost per task execution
    queries: int | None = Field(None, ge=0)  # source statements (read_source actions) per task execution
    max_steps: int = Field(4, ge=1, le=20)  # propose/execute rounds of the generic runtime


# The context compiler's knowledge sections (context/compiler.py KNOWLEDGE_SECTIONS). `catalog` is not
# knowledge: what an agent may see of the data is its authorized scope, not its contract.
KnowledgeSection = Literal["glossary", "business_rules", "metrics", "prior_findings", "negative_knowledge", "episodes",
                           "external"]


class AgentKnowledge(BaseModel):
    """Which knowledge sections an agent's prompts may carry, and how much (spec v3 §3.4 `knowledge`).
    The context compiler intersects each purpose profile with `sections` and caps its budget at
    `budget_chars`; an agent without a `knowledge` block receives no knowledge sections at all."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # spec v3 §3.4 writes this list as `purposes`; both spellings load
    sections: list[KnowledgeSection] = Field(default_factory=list, validation_alias=AliasChoices("sections", "purposes"))
    budget_chars: int | None = Field(None, ge=1_000, le=1_500_000)


class AgentOutputDecl(BaseModel):
    """One output an agent may persist (spec v3 §3.4 `output_contract`): an artifact type
    (`artifacts/registry.ARTIFACT_TYPES`) or a run record type (`OUTPUT_RECORD_TYPES`), and the schema its
    content must satisfy before it is written: `contract:<module>.<Model>` (a Pydantic contract under
    `analystos.contracts`), an inline JSON Schema, or none (any JSON object)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str
    schema_: str | dict[str, Any] | None = Field(None, alias="schema")


# Run records an agent writes besides artifacts; each persistence site checks its agent's contract.
OUTPUT_RECORD_TYPES = ("hypothesis", "experiment", "insight", "publication")


class AgentSpec(BaseModel):
    """The agent contract as the runtime, the tool gate and the admin API see it.

    A view derived from the `kind: Agent` capability manifest (`capabilities.agents.to_agent_spec`),
    never authored on its own: `agent_definition` rows cache it and carry only the platform-wide
    `enabled` switch. `name`/`description`/`capabilities`/`skills`/`model_profile` are the v1 names of
    the manifest's `role`/`goal`/`tags`/`skill.*` capabilities/first model purpose."""

    id: str
    capability_id: str | None = None
    version: str = "1.0"
    name: str
    description: str
    capabilities: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    model_profile: str = "none"
    model_purposes: list[str] = Field(default_factory=list)
    prompt_version: str = "v1"
    policies: AgentPolicies = Field(default_factory=AgentPolicies)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    knowledge: AgentKnowledge | None = None
    output_contract: list[AgentOutputDecl] = Field(default_factory=list)
    entry: str | None = None
    behaviours: dict[str, str] = Field(default_factory=dict)
    certification: str = "draft"
    source: str = "builtin"
    phase: str = "mvp"  # mvp | phase2 | ... ; seeds the platform-wide enabled flag (non-mvp agents start disabled)

    @property
    def knowledge_sections(self) -> list[str]:
        return list(self.knowledge.sections) if self.knowledge is not None else []

    def output(self, type_: str) -> AgentOutputDecl | None:
        return next((d for d in self.output_contract if d.type == type_), None)


class SkillSpec(BaseModel):
    id: str
    category: str
    description: str
    tools: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    deterministic: bool = True


class ToolSpec(BaseModel):
    tool_id: str
    name: str
    version: str = "1.0"
    description: str
    category: str
    capabilities: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    permission_scope: Literal["workspace", "source", "global"] = "workspace"
    risk: Risk = "low"
    cost_profile: Literal["low", "medium", "high"] = "low"
    latency_profile: Literal["low", "medium", "high"] = "low"
    # none: runs within policy. publish_only / always: an approval proposal must be approved first.
    approval_policy: Literal["none", "publish_only", "always"] = "none"
    side_effects: Literal["none", "internal_write", "external_write"] = "none"
    runtime: Literal["in_process", "sandbox", "remote_api"] = "in_process"
    min_role: Literal["viewer", "analyst", "editor", "owner"] = "analyst"
