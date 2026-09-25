"""Control-plane schema (FND-005/FND-009). Lives in the `analystos` database only.

The analytics plane (staged source data) is a different database reached through different
credentials; nothing in here is visible to user or model SQL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from analystos.db.base import Base

EMBEDDING_DIM = 256
JSON = JSONB


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "app_user"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # ABAC inputs (department, pii_clearance)
    created_at: Mapped[datetime] = _ts()


class Workspace(Base):
    __tablename__ = "workspace"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    objective: Mapped[str] = mapped_column(Text, default="")
    autonomy_level: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(20), default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceMember(Base):
    __tablename__ = "workspace_member"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # owner | editor | analyst | approver | viewer
    created_at: Mapped[datetime] = _ts()


class WorkspacePolicy(Base):
    """Immutable, versioned policy documents. workspace.policy_version points at the effective one."""

    __tablename__ = "workspace_policy"
    __table_args__ = (UniqueConstraint("workspace_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class Source(Base):
    __tablename__ = "source"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # postgres | sqlserver | csv | servicenow
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # never secret values
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)  # env:NAME | file:/path
    status: Mapped[str] = mapped_column(String(20), default="registered")
    # Where the gateway queries this source: "pushdown" (in place) or "staged" (analytics DB schema)
    execution_mode: Mapped[str] = mapped_column(String(20), default="staged")
    staging_schema: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class SourceAsset(Base):
    __tablename__ = "source_asset"
    __table_args__ = (UniqueConstraint("source_id", "schema_name", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    schema_name: Mapped[str] = mapped_column(String(120))  # schema where the gateway sees it
    name: Mapped[str] = mapped_column(String(200))
    source_name: Mapped[str] = mapped_column(String(200))  # name in the origin system (e.g. servicenow table)
    kind: Mapped[str] = mapped_column(String(20), default="table")
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    freshness_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Crawler state: structural fingerprint, lifecycle and deterministic semantics (role/domain/grain).
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(20), default="active", server_default="active")  # active | deprecated
    semantics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    description_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)  # source | rule | model | user
    business_name_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)  # source | rule | model | user
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What the staged snapshot is relative to its origin: rows staged, origin total, truncated, sampling (P4-C12)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _ts()


class SourceColumn(Base):
    __tablename__ = "source_column"
    __table_args__ = (UniqueConstraint("asset_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("source_asset.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    data_type: Mapped[str] = mapped_column(String(80))
    semantic_type: Mapped[str | None] = mapped_column(String(40), nullable=True)  # numeric|categorical|datetime|boolean|text|id
    nullable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)
    business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)  # pii, sensitive, restricted
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    semantics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")  # role, unit, pii, glossary
    # Tags set by a person are never removed by a crawl; crawler tags can only be added (tighten, never loosen).
    tags_origin: Mapped[str] = mapped_column(String(20), default="crawler", server_default="crawler")  # crawler | user


class Relationship(Base):
    __tablename__ = "relationship"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    from_asset_id: Mapped[str] = mapped_column(String(40))
    from_column: Mapped[str] = mapped_column(String(200))
    to_asset_id: Mapped[str] = mapped_column(String(40))
    to_column: Mapped[str] = mapped_column(String(200))
    cardinality: Mapped[str] = mapped_column(String(20), default="many_to_one")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(20), default="discovered")  # discovered | context | user
    created_at: Mapped[datetime] = _ts()


class ContextEntry(Base):
    """Semantic + episodic memory: glossary terms, definitions, metrics, rules, notes, prior-run episodes."""

    __tablename__ = "context_entry"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)  # null = global
    kind: Mapped[str] = mapped_column(String(30))  # term | definition | metric | rule | note | episode | dashboard
    name: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    synonyms: Mapped[list[str]] = mapped_column(JSON, default=list)
    mapped_columns: Mapped[list[str]] = mapped_column(JSON, default=list)  # "schema.table.column"
    origin: Mapped[str] = mapped_column(String(30), default="user")  # user | context2ai | agent
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = _ts()


class AgentDefinition(Base):
    __tablename__ = "agent_definition"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[str] = mapped_column(String(20))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WorkspaceCapability(Base):
    """Per-workspace enablement of a capability (spec v3 §3.1). No row = the default: on for the
    built-in set `playbook.investigate` needs (and built-in tools), off for everything else."""

    __tablename__ = "workspace_capability"
    __table_args__ = (UniqueConstraint("workspace_id", "capability_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    capability_id: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ToolDefinition(Base):
    __tablename__ = "tool_definition"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SkillDefinition(Base):
    __tablename__ = "skill_definition"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AnalysisRun(Base):
    __tablename__ = "analysis_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="NEW")
    autonomy_level: Mapped[int] = mapped_column(Integer, default=3)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan_version: Mapped[int] = mapped_column(Integer, default=0)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # authorized data scope snapshot
    instructions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # user redirects, in order
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # compiled filters from redirects
    control: Mapped[str] = mapped_column(String(20), default="run")  # run | pause | cancel
    requested_by: Mapped[str] = mapped_column(String(40))
    workflow_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # How the run was started: {"type": "user"} or {"type": "schedule", "schedule_id", "schedule_run_id",
    # "previous_run_id", "publish": "skip"|"propose", "report": {...}} or {"type": "alert", "alert_id"}
    origin: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Capability bindings (spec v3 §3.1, P4-X01): {"playbook": id, "refs": ["id@version", ...],
    # "manifests": {id: manifest}, "skipped": {step: reason}}. Bound when the plan materializes; the run
    # keeps these versions after a registry reload, and the refs are part of the plan hash.
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunTask(Base):
    __tablename__ = "run_task"
    __table_args__ = (UniqueConstraint("run_id", "key"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))  # stable idempotency key within the run
    agent_id: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30), default="NEW")
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # claim time
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bumped by every claim: a claim is a compare-and-set on it (P4-S02), and an attempt whose claim was
    # retaken (timed out, released) cannot write its result over the new attempt's.
    claim_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class RunEvent(Base):
    """Persisted event stream (§42). The SSE endpoint and activity feed read from here."""

    __tablename__ = "run_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = _ts()


class AgentMessage(Base):
    __tablename__ = "agent_message"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(30))  # thought | decision | observation | user
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class ModelCall(Base):
    __tablename__ = "model_call"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    purpose: Mapped[str] = mapped_column(String(60))
    profile: Mapped[str] = mapped_column(String(60))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_saved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # cache hits and deterministic skips
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Replay (P4-C09): content addresses of the redacted request and response in model_payload.
    request_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Execution ladder (P4-T01): the rung that answered — cache | registry | rules | decision | llm_small | llm_large.
    answered_by: Mapped[str] = mapped_column(String(20), default="llm_large", server_default="llm_large")
    # P4-T07: provider | price_table@<prices_version> | missing_price | none (no billable call).
    cost_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Prompt caching (P4-T04): prompt tokens the provider served from its cache (share = / input_tokens).
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Context compiler receipts (P4-T03): the context items the prompt carried ("context used").
    context_receipts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _ts()

    __table_args__ = (Index("ix_model_call_created_purpose_rung", "created_at", "purpose", "answered_by"),)


class ToolExecution(Base):
    __tablename__ = "tool_execution"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tool_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))  # ok | error | denied
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class QueryExecution(Base):
    __tablename__ = "query_execution"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor: Mapped[str] = mapped_column(String(80))  # user:<id> | agent:<id>
    purpose: Mapped[str] = mapped_column(String(120), default="analysis")
    sql: Mapped[str] = mapped_column(Text)
    executed_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # ok | rejected | error | timeout
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    referenced_assets: Mapped[list[str]] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    columns: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_preview: Mapped[list[Any]] = mapped_column(JSON, default=list)  # first rows (masked), for evidence UI
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()


class Hypothesis(Base):
    __tablename__ = "hypothesis"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    code: Mapped[str] = mapped_column(String(20))
    question: Mapped[str] = mapped_column(Text, default="")
    statement: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # executable analysis spec
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    priority_score: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    methods: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    iteration: Mapped[int] = mapped_column(Integer, default=1)
    origin: Mapped[str] = mapped_column(String(20), default="agent")  # agent | user | heuristic
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Experiment(Base):
    """One analysis method applied to one hypothesis (§44 experiment/analysis_result)."""

    __tablename__ = "experiment"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    method: Mapped[str] = mapped_column(String(60))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    query_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    role: Mapped[str] = mapped_column(String(20), default="primary")  # primary | verification
    created_at: Mapped[datetime] = _ts()


class Insight(Base):
    __tablename__ = "insight"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    code: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300))
    finding: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    population_size: Mapped[int] = mapped_column(BigInteger, default=0)
    business_impact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    caveats: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{type, id, label}]
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # REV record
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|verified|failed_verification|rejected
    narrative_source: Mapped[str] = mapped_column(String(160), default="template")  # llm | template
    created_at: Mapped[datetime] = _ts()


class Artifact(Base):
    __tablename__ = "artifact"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(300))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Plan version of the run that last wrote this artifact; a replan makes lower versions stale.
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    platform: Mapped[str | None] = mapped_column(String(40), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    creator_agent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    creator_user: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ArtifactVersion(Base):
    __tablename__ = "artifact_version"
    __table_args__ = (UniqueConstraint("artifact_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = _ts()


class LineageEdge(Base):
    """Generic provenance edge between any two persisted objects (artifact, insight, query, table...).
    Unique per workspace: name-identified nodes (``table``, ``src_x.orders``) recur across workspaces."""

    __tablename__ = "lineage_edge"
    # The unique key serves lookups by the `from` end; the index serves the `to` end (per-artifact lineage CTE).
    __table_args__ = (UniqueConstraint("workspace_id", "from_type", "from_id", "relation", "to_type", "to_id",
                                       name="uq_lineage_edge_workspace_edge"),
                      Index("ix_lineage_edge_workspace_to", "workspace_id", "to_type", "to_id"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    from_type: Mapped[str] = mapped_column(String(40))
    from_id: Mapped[str] = mapped_column(String(200))
    relation: Mapped[str] = mapped_column(String(40))
    to_type: Mapped[str] = mapped_column(String(40))
    to_id: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _ts()


class Approval(Base):
    """An approval binds to an immutable action proposal (§39): payload hash + plan hash + policy version."""

    __tablename__ = "approval"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(60))  # publish_dashboard | ...
    risk_tier: Mapped[str] = mapped_column(String(10))  # low | medium | high
    destination: Mapped[str | None] = mapped_column(String(120), nullable=True)
    affected_assets: Mapped[list[str]] = mapped_column(JSON, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[str] = mapped_column(String(80))  # the human who owns the run
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|expired|invalidated|executed
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class Publication(Base):
    """Each attempt to write to an external BI destination; enables reconcile-before-retry."""

    __tablename__ = "publication"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    destination: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(20))  # started | succeeded | partial | failed | rolled_back
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    user_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(30))  # redirect | add_context | reject_finding | deeper | edit_metric | edit_hypothesis
    text: Mapped[str] = mapped_column(Text, default="")
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts()


class AuditEvent(Base):
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)  # allow | deny | approval_required
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


# ------------------------------------------------------------------------------------------ Phase 3
class Schedule(Base):
    """Recurring work (§37). Executes with the owner's CURRENT permissions, re-checked at fire time."""

    __tablename__ = "schedule"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))  # reanalysis | dataset_refresh | report | monitor
    cron: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class ScheduleRun(Base):
    """One firing (SCH-005). `fire_key` makes concurrent schedulers and retries idempotent."""

    __tablename__ = "schedule_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedule.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    fire_key: Mapped[str] = mapped_column(String(120), unique=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(20), default="cron")  # cron | manual
    status: Mapped[str] = mapped_column(String(20), default="started")  # started | running | succeeded | failed | skipped
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Monitor(Base):
    """Continuous analytics (§38): a metric or data-quality signal evaluated on a schedule."""

    __tablename__ = "monitor"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))  # metric_threshold | metric_drift | change_point | data_quality
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_investigate: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown | ok | alerting | error
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class Alert(Base):
    __tablename__ = "alert"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    monitor_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    severity: Mapped[str] = mapped_column(String(10))  # info | warning | critical
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | acknowledged | resolved
    investigation_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _ts()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(Base):
    """In-app notifications. External delivery (email/webhook) is approval-gated (§39) and not in this release."""

    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)  # null = all members
    kind: Mapped[str] = mapped_column(String(40))  # alert | report | approval | run
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {type, id}
    read_by: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _ts()


class PlatformSetting(Base):
    """Append-only versions of the platform settings document (admin control plane)."""

    __tablename__ = "platform_setting"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class CrawlRun(Base):
    """One metadata crawl of a source (META-005/006): what was seen, what changed, what it cost."""

    __tablename__ = "crawl_run"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(20))  # full | incremental
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | schedule
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | succeeded | failed
    stage: Mapped[str] = mapped_column(String(40), default="discover")
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # CrawlDiff summary
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelPayload(Base):
    """Redacted model request/response bodies, content-addressed (P4-C09).

    A separate table rather than two JSON columns on model_call because the same body recurs:
    every retry and fallback attempt re-sends the identical request, cache hits repeat a stored
    response, and scheduled re-runs resend the same catalog. Addressing by sha256 of the canonical
    JSON stores each body once. Bodies are zlib-compressed and capped (llm/replay.py MAX_PAYLOAD_BYTES);
    an over-cap body is replaced by a stub and marked truncated, never cut mid-JSON."""

    __tablename__ = "model_payload"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # request | response
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)  # uncompressed canonical JSON size
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _ts()


class DecisionRecord(Base):
    """One DecisionService decision (ADR-0015, P4-T08): who decided, what every backend said, what the
    authority class enforced. `answer` is the enforced value; `proposal` the deciding backend's raw one."""

    __tablename__ = "decision"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    purpose: Mapped[str] = mapped_column(String(60), index=True)
    authority: Mapped[str] = mapped_column(String(30))
    backend: Mapped[str] = mapped_column(String(30))  # jev | rules | local_classifier | llm_structured | default
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(String(240), index=True, nullable=True)  # insight:<id> | alert:<key> | feedback:<id>
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    answer: Mapped[Any] = mapped_column(JSON, nullable=True)
    proposal: Mapped[Any] = mapped_column(JSON, nullable=True)
    probabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    enforced: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _ts()


class DecisionOutcome(Base):
    """A labelled outcome for a decision from a user signal (P4-T09): finding accepted/rejected/dismissed,
    alert acknowledged/dismissed, feedback class corrected. `label` is the true answer on the decision's
    own scale (an option key, or yes/no for probability purposes)."""

    __tablename__ = "decision_outcome"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decision.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(60), index=True)
    backend: Mapped[str] = mapped_column(String(30))
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    label: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(40))  # finding.accept | finding.reject | alert.dismiss | feedback.correct ...
    user_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _ts()


class DecisionCalibration(Base):
    """Append-only calibration evaluations and backend downgrades per purpose x backend (P4-T09).
    The latest row per (purpose, backend) is the effective state: `downgraded` rows remove that backend
    from the purpose's chain until a later evaluation or an administrator restores it."""

    __tablename__ = "decision_calibration"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    purpose: Mapped[str] = mapped_column(String(60), index=True)
    backend: Mapped[str] = mapped_column(String(30))
    window_days: Mapped[int] = mapped_column(Integer, default=30)
    n: Mapped[int] = mapped_column(Integer, default=0)
    brier: Mapped[float | None] = mapped_column(Float, nullable=True)
    ece: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_brier: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_ece: Mapped[float | None] = mapped_column(Float, nullable=True)
    downgraded: Mapped[bool] = mapped_column(Boolean, default=False)
    action: Mapped[str] = mapped_column(String(30))  # evaluated | downgraded | restored | admin_restored | admin_downgraded
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()


class McpServer(Base):
    """An external MCP server registered in one workspace (spec v3 §3.7, P4-X05).

    Nothing is called until an owner/admin sets `allowed`. `tools` is the last screened
    `tools/list` snapshot; `classifications` holds the owner's side-effect verdict per tool, bound to
    the tool's definition hash so a changed tool falls back to `write_external`.
    """

    __tablename__ = "mcp_server"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    url: Mapped[str] = mapped_column(String(500))
    transport: Mapped[str] = mapped_column(String(30), default="streamable_http")
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)  # env:NAME | file:/path, never a value
    status: Mapped[str] = mapped_column(String(20), default="registered")  # registered | ready | error
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # max_calls_per_run, timeout_seconds
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    classifications: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class McpClient(Base):
    """An external MCP client of AnalystOS (P4-X06). Only a SHA-256 of the secret is stored; the client
    acts as its own service user, so scope, roles and the gateway apply unchanged."""

    __tablename__ = "mcp_client"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # the public client_id
    name: Mapped[str] = mapped_column(String(200))
    secret_hash: Mapped[str] = mapped_column(String(64))
    service_user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | revoked
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpGrant(Base):
    """What one MCP client may do in one workspace: role, tools and per-tool daily quotas."""

    __tablename__ = "mcp_grant"
    __table_args__ = (UniqueConstraint("client_id", "workspace_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("mcp_client.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # viewer | analyst
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    quotas: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)  # tool -> calls per UTC day
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class McpUsage(Base):
    """Per-day call counters for quota enforcement (incremented atomically before a call runs)."""

    __tablename__ = "mcp_usage"
    __table_args__ = (UniqueConstraint("client_id", "workspace_id", "tool", "day"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("mcp_client.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(40))
    tool: Mapped[str] = mapped_column(String(80))
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (UTC)
    count: Mapped[int] = mapped_column(Integer, default=0)


# ------------------------------------------------------------------------------ registries (P4-T05)
class VerifiedQuery(Base):
    """A parameterised, verified query for Ask (ladder rung L1, ADR-0012). Promoted from a successful
    Ask answer or a verified finding; `sql_template` holds `{{name}}` placeholders for `parameters`
    (typed: enum over a profiled column's vocabulary, date, number, string). Executed through the
    gateway under the asking user's scope, so a registry hit never widens what anyone can read."""

    __tablename__ = "verified_query"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    patterns: Mapped[list[str]] = mapped_column(JSON, default=list)  # question phrasings (parameter values removed)
    sql_template: Mapped[str] = mapped_column(Text)
    parameters: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dialect: Mapped[str] = mapped_column(String(40), default="postgres")
    origin: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {type: ask|finding, query_id, insight_id, question}
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # the AnalysisSpec of a promoted finding
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | retired
    hits: Mapped[int] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RegisteredHypothesis(Base):
    """The hypothesis registry (review C11): every tested hypothesis spec of a workspace, keyed by its
    spec hash, with the claim identity of the method registry. Scheduled re-analysis replays it with
    no model call, so "new" and "resolved" mean the evidence changed, not that the questions did."""

    __tablename__ = "registered_hypothesis"
    __table_args__ = (UniqueConstraint("workspace_id", "spec_hash"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    spec_hash: Mapped[str] = mapped_column(String(64))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    question: Mapped[str] = mapped_column(Text, default="")
    statement: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(String(60))
    asset: Mapped[str] = mapped_column(String(300))
    question_key: Mapped[str] = mapped_column(String(64), index=True)  # hash of the claim key without its top group
    claim: Mapped[list[Any]] = mapped_column(JSON, default=list)  # last claim key (method registry), top group last
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | retired
    last_outcome: Mapped[str] = mapped_column(String(30), default="")  # verified | supported | rejected | inconclusive | ...
    last_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(20), default="agent")  # origin of the first registration
    first_run_id: Mapped[str] = mapped_column(String(40))
    last_run_id: Mapped[str] = mapped_column(String(40))
    times_tested: Mapped[int] = mapped_column(Integer, default=0)
    times_verified: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BuildTarget(Base):
    """A schema the workspace owner designated for builds (P4-E06). The BuildGateway writes nowhere
    else: its per-workspace build role holds CREATE on these schemas only, and sources stay read-only."""

    __tablename__ = "build_target"
    __table_args__ = (UniqueConstraint("workspace_id", "engine", "schema_name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    engine: Mapped[str] = mapped_column(String(80))  # postgres:analytics
    schema_name: Mapped[str] = mapped_column(String(63))
    build_role: Mapped[str] = mapped_column(String(63))  # NOLOGIN role the builder login SETs for this workspace
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | retired
    provisioning: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()


class BuildJob(Base):
    """One generated dbt project and its execution (P4-E04): the project files and their hash, the dry
    run and cost estimate, the approval that covers it, the rollback plan, the harvested manifest,
    run results and OpenLineage events. Provenance: dataset/metrics -> transformation -> job -> tables."""

    __tablename__ = "build_job"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)  # the elt_build run
    source_run_id: Mapped[str] = mapped_column(String(40), index=True)  # the run whose dataset and KPIs are built
    artifact_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # the `transformation` artifact
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    engine: Mapped[str] = mapped_column(String(80))
    runner: Mapped[str] = mapped_column(String(40), default="dbt-core")
    target_schema: Mapped[str] = mapped_column(String(63))
    project_name: Mapped[str] = mapped_column(String(120))
    project_files: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    project_hash: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relations: Mapped[list[str]] = mapped_column(JSON, default=list)  # schema.relation the job creates or replaces
    dry_run: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rollback: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned|awaiting_approval|running|succeeded|failed|refused
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # harvested summary of target/manifest.json
    run_results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    openlineage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    log_tail: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
