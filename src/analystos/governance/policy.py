"""Governance engine (§45-§46). Authorization is decided here, on the server, from persisted state.
Prompts, retrieved context and model output never widen what this returns."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.policy import DataScope, ExecutionIdentity, PolicyDecision, WorkspacePolicyDoc
from analystos.contracts.registry import ToolSpec
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, Forbidden, NotFound
from analystos.db.models import Source, SourceAsset, SourceColumn, User, Workspace, WorkspaceMember, WorkspacePolicy
from analystos.governance.audit import audit
from analystos.security.auth import APPROVER_ROLES, role_at_least


def source_dialect(kind: str, execution_mode: str | None) -> str:
    """Dialect the gateway validates a source's SQL in (config/source_kinds.yaml); staged -> postgres."""
    from analystos.connectors.kinds import dialect_for

    try:
        return dialect_for(kind, execution_mode)
    except AnalystOSError:
        return "postgres"

# Autonomy ceiling in the initial release (§39): publication, scheduling, external notification
# and source mutation always need an approval regardless of workspace autonomy level.
ALWAYS_APPROVAL_ACTIONS = {"publish", "schedule", "notify_external", "export"}
NEVER_ALLOWED_ACTIONS = {"source_mutation"}


def get_workspace(session: Session, workspace_id: str) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.deleted_at is not None:
        raise NotFound(f"workspace {workspace_id} not found")
    return ws


def member_role(session: Session, user: User, workspace_id: str) -> str | None:
    if user.is_admin:
        return "owner"
    return session.scalar(select(WorkspaceMember.role).where(
        WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id))


def require_role(session: Session, user: User, workspace_id: str, minimum: str) -> str:
    get_workspace(session, workspace_id)
    role = member_role(session, user, workspace_id)
    if role is None:
        # Do not reveal existence of workspaces the caller cannot see.
        raise NotFound(f"workspace {workspace_id} not found")
    if not role_at_least(role, minimum) and not (minimum == "approver" and role in APPROVER_ROLES):
        raise Forbidden(f"role '{role}' cannot perform this action (needs {minimum})")
    return role


def load_policy(session: Session, workspace: Workspace) -> WorkspacePolicyDoc:
    row = session.scalar(select(WorkspacePolicy).where(
        WorkspacePolicy.workspace_id == workspace.id, WorkspacePolicy.version == workspace.policy_version))
    doc = WorkspacePolicyDoc.model_validate(row.document) if row else WorkspacePolicyDoc()
    settings = get_settings()
    # Platform ceilings: a workspace can tighten, never loosen.
    doc.max_rows = min(doc.max_rows, settings.query_max_rows)
    doc.query_timeout_seconds = min(doc.query_timeout_seconds, settings.query_timeout_seconds)
    doc.publish_requires_approval = True
    return doc


def save_policy(session: Session, workspace: Workspace, doc: WorkspacePolicyDoc, user_id: str) -> int:
    version = (session.scalar(select(WorkspacePolicy.version).where(WorkspacePolicy.workspace_id == workspace.id)
                              .order_by(WorkspacePolicy.version.desc()).limit(1)) or 0) + 1
    session.add(WorkspacePolicy(workspace_id=workspace.id, version=version, document=doc.model_dump(), created_by=user_id))
    workspace.policy_version = version
    return version


def _matches(pattern: str, fq: str) -> bool:
    if pattern.startswith("*."):
        return fq.rsplit(".", 1)[-1] == pattern[2:]
    return pattern == fq


PII_ORDER = {"none": 0, "restricted": 1, "allowed": 2}


def resolve_scope(session: Session, user: User, workspace_id: str, *, source_ids: list[str] | None = None,
                  minimum_role: str = "analyst", pii_access: str | None = None) -> DataScope:
    """Server-side authorized data scope for this caller (§12.2). Selected assets only.
    `pii_access` (an agent manifest's policy) can only tighten the workspace policy."""
    role = require_role(session, user, workspace_id, minimum_role)
    workspace = get_workspace(session, workspace_id)
    policy = load_policy(session, workspace)
    if pii_access is not None and PII_ORDER[pii_access] < PII_ORDER[policy.pii_access]:
        policy.pii_access = pii_access
    sources = list(session.scalars(select(Source).where(Source.workspace_id == workspace_id, Source.status == "ready")))
    if source_ids:
        sources = [s for s in sources if s.id in source_ids]
    pii_cleared = policy.pii_access == "allowed" or (
        policy.pii_access == "restricted" and bool((user.attributes or {}).get("pii_clearance")))
    scope = DataScope(workspace_id=workspace_id, user_id=user.id, role=role, max_rows=policy.max_rows,
                      timeout_seconds=policy.query_timeout_seconds, policy_version=workspace.policy_version)
    for source in sources:
        scope.source_ids.append(source.id)
        scope.source_dialects[source.id] = source_dialect(source.kind, source.execution_mode)
        assets = session.scalars(select(SourceAsset).where(SourceAsset.source_id == source.id, SourceAsset.selected.is_(True)))
        for asset in assets:
            fq = f"{asset.schema_name}.{asset.name}"
            scope.assets.append(fq)
            scope.asset_sources[fq] = source.id
            cols = list(session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal)))
            scope.columns[fq] = [c.name for c in cols]
            for col in cols:
                col_fq = f"{fq}.{col.name}"
                tags = set(col.tags or [])
                restricted = "restricted" in tags or any(_matches(p, col_fq) for p in policy.restricted_columns)
                pii = "pii" in tags or any(_matches(p, col_fq) for p in policy.pii_columns)
                if restricted or (pii and not pii_cleared):
                    scope.denied_columns.append(col_fq)
    return scope


def evaluate(session: Session, user: User, identity: ExecutionIdentity, action: str, *,
             tool: ToolSpec | None = None, destination: str | None = None, autonomy_level: int | None = None,
             record: bool = True) -> PolicyDecision:
    """allow | deny | approval_required with machine-readable reasons. Re-evaluated at execution time."""
    workspace = get_workspace(session, identity.workspace_id)
    policy = load_policy(session, workspace)
    role = member_role(session, user, identity.workspace_id)
    level = workspace.autonomy_level if autonomy_level is None else min(autonomy_level, workspace.autonomy_level)
    reasons: list[str] = []
    decision = "allow"
    risk = "low"

    if role is None or not user.active:
        decision, reasons = "deny", ["not_a_workspace_member"]
    elif action in NEVER_ALLOWED_ACTIONS:
        decision, reasons, risk = "deny", ["source_systems_are_read_only_in_this_release"], "high"
    else:
        minimum = tool.min_role if tool else ("editor" if action in ALWAYS_APPROVAL_ACTIONS else "analyst")
        if not role_at_least(role, minimum):
            decision, reasons = "deny", [f"role_{role}_below_{minimum}"]
        if tool is not None:
            risk = tool.risk
            if tool.tool_id in policy.tool_denylist:
                decision, reasons = "deny", [*reasons, "tool_denied_by_workspace_policy"]
        if action in {"publish", "export"} and destination and destination not in policy.publish_destinations:
            decision, reasons, risk = "deny", [*reasons, f"destination_{destination}_not_allowed"], "high"
        if decision == "allow":
            if action in ALWAYS_APPROVAL_ACTIONS or (tool and tool.approval_policy == "always") or \
                    (tool and tool.approval_policy == "publish_only" and action == "publish"):
                decision, risk = "approval_required", "high" if action == "publish" else "medium"
                reasons.append("initial_release_requires_approval_for_external_side_effects")
            elif level <= 0 and action in {"run_analysis", "tool"}:
                decision, reasons = "deny", ["autonomy_level_0_manual_only"]
            elif level == 1 and action == "run_analysis":
                decision = "approval_required"
                reasons.append("autonomy_level_1_agent_recommends_only")
            elif level == 2 and action == "run_analysis":
                decision = "approval_required"
                reasons.append("autonomy_level_2_executes_after_plan_approval")
            else:
                reasons.append(f"within_policy_autonomy_{level}")
    result = PolicyDecision(decision=decision, reasons=reasons, risk_tier=risk, policy_version=workspace.policy_version,
                            effective_autonomy=level, details={"role": role, "action": action,
                                                               "tool": tool.tool_id if tool else None,
                                                               "destination": destination, "agent": identity.agent_id,
                                                               "purpose": identity.purpose})
    if record:
        audit(f"user:{user.id}" if not identity.agent_id else f"agent:{identity.agent_id}", f"policy.{action}",
              workspace_id=identity.workspace_id, run_id=identity.run_id, target=tool.tool_id if tool else destination,
              decision=decision, reasons=reasons, details=result.details, session=session)
    return result
