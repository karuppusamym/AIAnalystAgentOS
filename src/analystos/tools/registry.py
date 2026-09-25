"""Tool registry (TLR-001..003). Tools are declared here, persisted to tool_definition, and every
invocation passes through `ToolRuntime.invoke`: registered + enabled, bound to the calling agent,
permitted by workspace policy, and audited with inputs/outputs."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.policy import ExecutionIdentity, PolicyDecision
from analystos.contracts.registry import AgentSpec, ToolSpec
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, ApprovalRequired, NotFound, PolicyDenied
from analystos.db.base import session_scope
from analystos.db.models import AgentDefinition, SkillDefinition, ToolDefinition, User
from analystos.events.bus import emit
from analystos.governance.policy import evaluate

BUILTIN_TOOLS: list[ToolSpec] = [
    ToolSpec(tool_id="metadata.read", name="Read source metadata", category="metadata",
             description="Read discovered schemas, tables, columns, keys and statistics for selected assets."),
    ToolSpec(tool_id="relationships.discover", name="Discover relationships", category="metadata",
             description="Infer and validate joins between selected tables (declared references, name heuristics, containment)."),
    ToolSpec(tool_id="sql.execute", name="Governed SQL executor", category="sql", risk="medium", cost_profile="medium",
             latency_profile="medium", description="Execute a read-only SQL query through the data access gateway within the authorized scope."),
    ToolSpec(tool_id="profile.table", name="Profile table", category="dataframe", cost_profile="medium",
             description="Pushdown profiling of a table: nulls, distinct, quantiles, histograms, categories, time coverage."),
    ToolSpec(tool_id="quality.check", name="Data quality checks", category="data_quality",
             description="Detect missing values, duplicate keys, orphan references, temporal-order violations and future timestamps."),
    ToolSpec(tool_id="analysis.run", name="Run statistical analysis", category="statistics", cost_profile="medium",
             description="Compile an analysis spec to SQL, fetch governed results and run the statistical test for the method."),
    ToolSpec(tool_id="analysis.verify", name="Independent verification", category="statistics",
             description="Re-run the evidence query and apply an independent second method to confirm a finding."),
    ToolSpec(tool_id="python.execute", name="Python sandbox", category="dataframe", risk="medium", runtime="sandbox",
             description="Run allow-listed numeric Python on governed query results in a resource-limited sandbox."),
    ToolSpec(tool_id="context.search", name="Context search", category="context",
             description="Layered retrieval over Context2AI / local glossary, metrics, prior episodes and graph neighborhood."),
    ToolSpec(tool_id="graph.neighborhood", name="Graph neighborhood", category="graph",
             description="Neighbourhood of tables (joins and prior findings) from the Postgres lineage graph; Neo4j when enabled."),
    ToolSpec(tool_id="viz.chart", name="Chart designer", category="visualization",
             description="Select chart type and encodings for an analytical intent and compute a governed preview."),
    ToolSpec(tool_id="artifact.write", name="Artifact writer", category="observability", side_effects="internal_write",
             description="Persist and version artifacts with lineage."),
    ToolSpec(tool_id="governance.review", name="Governance review", category="governance",
             description="Check an externally visible bundle against restricted columns, scope and destination policy."),
    ToolSpec(tool_id="preview.publish", name="Preview publish", category="bi_publishing", side_effects="internal_write",
             description="Validate a publish bundle and render it locally without an external BI tool."),
    ToolSpec(tool_id="crawl.run", name="Metadata crawl", category="metadata", side_effects="internal_write", min_role="editor",
             description="Full or incremental metadata crawl of a source: fingerprints, schema drift, deterministic semantics, "
                         "PII classification, declared relationships, glossary links, context store and graph."),
    ToolSpec(tool_id="metadata.enrich", name="Metadata enrichment", category="metadata", cost_profile="medium",
             description="Optional model descriptions for tables the rules could not describe confidently; screened, batched, "
                         "never overrides reviewed or user-written metadata."),
    ToolSpec(tool_id="sql.explain", name="Explain SQL", category="sql",
             description="Deterministic explanation of a SQL statement (tables, joins, filters, grouping, aggregations) and the "
                         "gateway validator's verdict, without executing it."),
    ToolSpec(tool_id="forecast.run", name="Forecast", category="statistics",
             description="Exponential-smoothing forecast with simulated prediction intervals; deviation of the latest period "
                         "from its forecast."),
    ToolSpec(tool_id="superset.publish", name="Publish to Superset", category="bi_publishing", risk="high",
             approval_policy="publish_only", side_effects="external_write", runtime="remote_api", min_role="editor",
             capabilities=["dataset", "chart", "dashboard", "filters"],
             description="Create or update datasets, metrics, charts and dashboards in Apache Superset."),
]
TOOLS = {t.tool_id: t for t in BUILTIN_TOOLS}


def load_agent_specs(directory: Path | None = None) -> list[AgentSpec]:
    specs = []
    for path in sorted((directory or get_settings().agents_dir).glob("*.yaml")):
        data = yaml.safe_load(path.read_text())["agent"]
        specs.append(AgentSpec.model_validate(data))
    return specs


def seed_registries(session: Session) -> None:
    for tool in BUILTIN_TOOLS:
        row = session.get(ToolDefinition, tool.tool_id)
        if row is None:
            session.add(ToolDefinition(id=tool.tool_id, spec=tool.model_dump()))
        else:
            row.spec = tool.model_dump()
    for spec in load_agent_specs():
        row = session.get(AgentDefinition, spec.id)
        if row is None:
            session.add(AgentDefinition(id=spec.id, version=spec.version, spec=spec.model_dump(), enabled=spec.phase == "mvp"))
        elif row.version != spec.version:
            row.version, row.spec = spec.version, spec.model_dump()
    from analystos.skills.registry import SKILLS

    for skill in SKILLS:
        sid = skill["id"]
        row = session.get(SkillDefinition, sid)
        clean = {k: (v if isinstance(v, (str, int, float, bool, list, dict, type(None))) else str(v)) for k, v in skill.items()}
        if row is None:
            session.add(SkillDefinition(id=sid, spec=clean))
        else:
            row.spec = clean


def get_agent_spec(session: Session, agent_id: str) -> AgentSpec:
    row = session.get(AgentDefinition, agent_id)
    if row is None or not row.enabled:
        raise NotFound(f"agent {agent_id} is not registered or disabled")
    return AgentSpec.model_validate(row.spec)


def _summarize(value: Any, limit: int = 2000) -> Any:
    if isinstance(value, dict):
        return {k: _summarize(v, limit // 4) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_summarize(v, limit // 4) for v in value[:20]] + ([f"...{len(value) - 20} more"] if len(value) > 20 else [])
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    if hasattr(value, "model_dump"):
        return _summarize(value.model_dump(), limit)
    return value


class ToolRuntime:
    """Per-step tool gate for one agent."""

    def __init__(self, *, user: User, identity: ExecutionIdentity, agent: AgentSpec) -> None:
        self.user = user
        self.identity = identity
        self.agent = agent

    def authorize(self, tool_id: str, inputs: dict[str, Any] | None = None, *, bound: bool = True) -> PolicyDecision | None:
        """Policy-check a tool use without running it; a denial is recorded and raised.

        ``bound=False`` is for the implicit paths that act *as* a tool without being invoked by name
        (skill SQL as ``sql.execute``, artifact persistence as ``artifact.write``, spec v2 §6): the
        workspace policy (denylist, role, autonomy) and the tool's enabled flag apply, the per-agent
        binding does not, because the agent's bound tool (``profile.table``...) is what it invoked."""
        inputs = inputs or {}
        with session_scope() as session:
            row = session.get(ToolDefinition, tool_id)
            tool = ToolSpec.model_validate(row.spec) if row else None
            reasons: list[str] = []
            if tool is None or not row.enabled:
                reasons.append("tool_not_registered_or_disabled")
            elif bound and tool_id not in self.agent.tools:
                reasons.append(f"tool_not_bound_to_agent_{self.agent.id}")
            elif tool_id == "python.execute" and not _platform().features.python_sandbox:
                reasons.append("python_sandbox_disabled_by_admin")
            decision = None
            if not reasons:
                identity = self.identity.model_copy(update={"tool_id": tool_id, "agent_id": self.agent.id})
                action = "publish" if tool.side_effects == "external_write" else "tool"
                decision = evaluate(session, session.merge(self.user), identity, action, tool=tool,
                                    destination=tool_id.split(".")[0] if action == "publish" else None)
                if decision.decision == "deny":
                    reasons += decision.reasons
            if reasons:
                session.add(_execution(self, tool_id, "denied", inputs, {}, {"decision": "deny", "reasons": reasons}, 0, None))
                emit(self.identity.workspace_id, "policy.denied", {"tool": tool_id, "agent": self.agent.id, "reasons": reasons},
                     run_id=self.identity.run_id, session=session)
        if reasons:
            raise PolicyDenied(f"tool {tool_id} denied for agent {self.agent.id}: {', '.join(reasons)}")
        if decision is not None and decision.decision == "approval_required" and not inputs.get("_approval_verified"):
            raise ApprovalRequired(f"tool {tool_id} requires an approved proposal")
        return decision

    def invoke(self, tool_id: str, inputs: dict[str, Any], fn: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        decision = self.authorize(tool_id, inputs)
        status, output, error = "ok", None, None
        try:
            output = fn()
            return output
        except AnalystOSError as exc:
            status, error = "error", f"{exc.code}: {exc.message}"
            raise
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            latency = round((time.perf_counter() - started) * 1000)
            with session_scope() as session:
                session.add(_execution(self, tool_id, status, inputs, _summarize(output) if output is not None else {},
                                       {"decision": decision.decision if decision else None,
                                        "reasons": decision.reasons if decision else []}, latency, error))


def gate_agent_write(session: Session, *, workspace_id: str, run_id: str, agent_id: str, tool_id: str = "artifact.write",
                     inputs: dict[str, Any] | None = None) -> None:
    """Tool gate for artifact persistence by an agent inside a run (``save_artifact``). Runs its own
    short transaction so a denial is recorded even when the caller's transaction rolls back."""
    from analystos.db.models import AnalysisRun

    run = session.get(AnalysisRun, run_id)
    if run is None:
        return
    with session_scope() as s:
        user = s.get(User, run.requested_by)
        row = s.get(AgentDefinition, agent_id)
        spec = AgentSpec.model_validate(row.spec) if row else AgentSpec.model_validate(
            {"id": agent_id, "name": agent_id, "description": "", "tools": []})
        s.expunge_all()
    if user is None:
        raise PolicyDenied("run owner no longer exists")
    identity = ExecutionIdentity(user_id=user.id, workspace_id=workspace_id, agent_id=agent_id, purpose="analysis",
                                 run_id=run_id)
    ToolRuntime(user=user, identity=identity, agent=spec).authorize(tool_id, inputs, bound=False)


def _execution(rt: ToolRuntime, tool_id, status, inputs, output, decision, latency, error):
    from analystos.db.models import ToolExecution

    out = output if isinstance(output, dict) else {"value": output}
    return ToolExecution(workspace_id=rt.identity.workspace_id, run_id=rt.identity.run_id, task_id=rt.identity.task_id,
                         agent_id=rt.agent.id, tool_id=tool_id, status=status, input=_summarize(inputs), output=out,
                         decision=decision, latency_ms=latency, error=error)


def _platform():
    from analystos.services.platform_settings import get

    return get()


def list_tools(session: Session) -> list[dict]:
    return [{**r.spec, "enabled": r.enabled} for r in session.scalars(select(ToolDefinition).order_by(ToolDefinition.id))]
