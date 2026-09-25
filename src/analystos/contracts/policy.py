"""Policy model (FND-011), data scope and governance decisions (§45-§46)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Decision = Literal["allow", "deny", "approval_required"]


class WorkspacePolicyDoc(BaseModel):
    """Versioned per-workspace policy. Values here may only tighten the platform ceilings."""

    max_rows: int = 50_000
    query_timeout_seconds: int = 30
    max_queries_per_run: int = 200
    run_token_budget: int = 400_000
    run_cost_budget_usd: float = 2.0
    workspace_monthly_cost_budget_usd: float = 50.0
    expensive_model_approval_usd: float = 1.0
    allowed_models: list[str] = Field(default_factory=list)  # empty = platform allowlist
    allowed_providers: list[str] = Field(default_factory=lambda: ["openrouter", "typesafe"])
    data_residency: str | None = None  # informational until providers expose region metadata
    send_data_samples_to_models: bool = False  # only aggregates/stats/schema leave the platform by default
    restricted_columns: list[str] = Field(default_factory=list)  # "schema.table.column" or "*.column"
    pii_columns: list[str] = Field(default_factory=list)
    pii_access: Literal["none", "restricted", "allowed"] = "restricted"
    tool_denylist: list[str] = Field(default_factory=list)
    publish_destinations: list[str] = Field(default_factory=lambda: ["superset"])
    publish_requires_approval: bool = True  # cannot be disabled in the MVP (enforced in code)
    separation_of_duties: bool = False
    approval_ttl_hours: int = 72
    max_iterations: int = 3
    alpha: float = 0.05
    # Domain packs (spec v3 §3.6) whose templates and knowledge runs use: None = every installed pack
    # whose applies_when matches the selected catalog; a list (even empty) = exactly these pack ids.
    domain_packs: list[str] | None = None


class DataScope(BaseModel):
    """The resolved, server-side authorized scope carried through a run (§12.2 invariants).

    assets: fully qualified "schema.table" names the caller may reference.
    denied_columns: "schema.table.column" never readable (restricted + PII without clearance).
    """

    workspace_id: str
    user_id: str
    role: str
    source_ids: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    asset_sources: dict[str, str] = Field(default_factory=dict)  # "schema.table" -> source_id
    denied_columns: list[str] = Field(default_factory=list)
    # "schema.table" -> ordered column names. The gateway uses it to expand stars and resolve
    # every column reference before execution; unknown columns are rejected.
    columns: dict[str, list[str]] = Field(default_factory=dict)
    source_dialects: dict[str, str] = Field(default_factory=dict)  # source_id -> sqlglot dialect
    max_rows: int = 50_000
    timeout_seconds: int = 30
    policy_version: int = 1

    def scope_hash(self) -> str:
        from analystos.core.ids import stable_hash

        return stable_hash(self.model_dump())


class PolicyDecision(BaseModel):
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    risk_tier: Literal["low", "medium", "high"] = "low"
    policy_version: int = 1
    effective_autonomy: int = 0
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class ExecutionIdentity(BaseModel):
    """Who is acting, for what (§46 example execution identity)."""

    user_id: str
    workspace_id: str
    agent_id: str | None = None
    purpose: str = "analysis"
    tool_id: str | None = None
    source_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
