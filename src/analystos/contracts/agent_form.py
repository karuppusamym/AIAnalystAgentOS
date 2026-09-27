"""The agent form (P7-19): a declarative agent described by a workspace owner instead of YAML.

The form is input only. `capabilities/agent_forms.compile_form` turns it into the same `kind: Agent`
manifest the YAML path loads (`capabilities/agents.py`), and the `agent` definition kind validates
that manifest, including the workspace grant check, whichever route it arrives by. Nothing in the
form can name a python entry, a tool, a certification or a model purpose: those are derived or fixed.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analystos.contracts.registry import KnowledgeSection

AGENT_KEY = re.compile(r"^agent\.[a-z][a-z0-9_]{1,59}$")
AutonomyMode = Literal["deterministic", "propose"]
PiiAccess = Literal["none", "restricted", "allowed"]
PII_RANK: dict[str, int] = {"none": 0, "restricted": 1, "allowed": 2}
MAX_LLM_CALLS = 20
FORM_OUTPUT_TYPES = ("agent_output",)  # the generic runtime writes one artifact shape (agents.GENERIC_OUTPUT_SCHEMA)


class AgentFormBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_calls: int = Field(2, ge=0, le=MAX_LLM_CALLS)
    usd: float = Field(0.05, ge=0)
    queries: int = Field(0, ge=0)
    max_steps: int = Field(3, ge=1, le=20)


class AgentFormKnowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: list[KnowledgeSection] = Field(default_factory=list)
    budget_chars: int | None = Field(None, ge=1_000, le=200_000)


class AgentFormOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["agent_output"] = "agent_output"
    name: str = Field(min_length=3, max_length=120)


class AgentFormAutonomy(BaseModel):
    """How far the agent acts on its own: only its default actions, or model proposals among its
    capabilities (every proposal is still validated in code); and what data it may see."""

    model_config = ConfigDict(extra="forbid")

    mode: AutonomyMode = "deterministic"
    pii_access: PiiAccess = "none"
    max_rows: int = Field(1_000, ge=1, le=500_000)


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str = Field(min_length=3, max_length=120)
    purpose: str = Field(min_length=10, max_length=2_000)
    capabilities: list[str] = Field(min_length=1, max_length=20)
    default_actions: list[str] = Field(default_factory=list, max_length=5)
    knowledge: AgentFormKnowledge = Field(default_factory=AgentFormKnowledge)
    output: AgentFormOutput
    budget: AgentFormBudget = Field(default_factory=AgentFormBudget)
    autonomy: AgentFormAutonomy = Field(default_factory=AgentFormAutonomy)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not AGENT_KEY.match(v):
            raise ValueError("key must look like agent.<name>: lowercase letters, digits and '_'")
        return v

    @field_validator("capabilities", "default_actions")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("list each capability once")
        return v
