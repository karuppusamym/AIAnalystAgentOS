"""Agent, skill and tool contracts (§12, §14, §15)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Risk = Literal["low", "medium", "high"]


class AgentPolicies(BaseModel):
    max_rows_extract: int = 500_000
    pii_access: Literal["none", "restricted", "allowed"] = "restricted"
    approval_for_publish: bool = True
    max_iterations: int = 3


class AgentSpec(BaseModel):
    id: str
    version: str = "1.0"
    name: str
    description: str
    capabilities: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    model_profile: str = "analytical_reasoning"
    prompt_version: str = "v1"
    policies: AgentPolicies = Field(default_factory=AgentPolicies)
    verification_required: bool = False
    phase: str = "mvp"  # mvp | phase2 | ... ; non-mvp agents are registered but not planned


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
