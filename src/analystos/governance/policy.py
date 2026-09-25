"""Governance engine (§45-§46). Authorization is decided here, on the server, from persisted state.
Prompts, retrieved context and model output never widen what this returns."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.policy import AttributeRule, DataScope, ExecutionIdentity, PolicyDecision, WorkspacePolicyDoc
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
ALWAYS_APPROVAL_ACTIONS = {"publish", "schedule", "notify_external", "export", "build"}
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


def _selected_assets(session: Session, source_ids: list[str]) -> tuple[dict[str, list[tuple[str, str]]],
                                                                       dict[str, list[tuple[str, list[str] | None]]]]:
    """Selected assets per source as (asset id, `schema.name`) and their columns in ordinal order as
    (name, tags): two reads for any number of sources and assets (P4-S02; was one per source and asset)."""
    if not source_ids:
        return {}, {}
    in_scope = (SourceAsset.source_id.in_(source_ids), SourceAsset.selected.is_(True))
    assets: dict[str, list[tuple[str, str]]] = {}
    for asset_id, source_id, schema_name, name in session.execute(
            select(SourceAsset.id, SourceAsset.source_id, SourceAsset.schema_name, SourceAsset.name).where(*in_scope)):
        assets.setdefault(source_id, []).append((asset_id, f"{schema_name}.{name}"))
    columns: dict[str, list[tuple[str, list[str] | None]]] = {}
    for asset_id, name, tags in session.execute(
            select(SourceColumn.asset_id, SourceColumn.name, SourceColumn.tags)
            .join(SourceAsset, SourceAsset.id == SourceColumn.asset_id).where(*in_scope)
            .order_by(SourceColumn.asset_id, SourceColumn.ordinal, SourceColumn.id)):
        columns.setdefault(asset_id, []).append((name, tags))
    return assets, columns


def attributes_satisfy(attributes: dict | None, require: dict[str, list[str]]) -> bool:
    """Every required attribute present with an accepted value (list-valued attributes: any element)."""
    attributes = attributes or {}
    for key, accepted in require.items():
        value = attributes.get(key)
        values = value if isinstance(value, list) else [value]
        if not {str(v) for v in values if v is not None} & {str(a) for a in accepted}:
            return False
    return True


def apply_attribute_rules(scope: DataScope, attributes: dict | None, rules: list[AttributeRule],
                          column_tags: dict[str, set[str]] | None = None) -> list[str]:
    """ABAC (SEC-003) on a resolved scope: every rule the caller's attributes do not satisfy removes
    its assets and denies its columns. Only ever narrows. Returns the ids of the rules applied."""
    applied = []
    column_tags = column_tags or {}
    for rule in rules:
        if attributes_satisfy(attributes, rule.require):
            continue
        applied.append(rule.id or "unnamed")
        dropped = [fq for fq in scope.assets if any(_matches(p, fq) for p in rule.assets)]
        for fq in dropped:
            scope.assets.remove(fq)
            scope.asset_sources.pop(fq, None)
            scope.columns.pop(fq, None)
        for fq, cols in scope.columns.items():
            for name in cols:
                col_fq = f"{fq}.{name}"
                if col_fq in scope.denied_columns:
                    continue
                if any(_matches(p, col_fq) for p in rule.columns) or (column_tags.get(col_fq, set()) & set(rule.column_tags)):
                    scope.denied_columns.append(col_fq)
        scope.denied_columns = [c for c in scope.denied_columns if c.rsplit(".", 1)[0] in scope.columns]
    return applied


def resolve_scope(session: Session, user: User, workspace_id: str, *, source_ids: list[str] | None = None,
                  minimum_role: str = "analyst", pii_access: str | None = None) -> DataScope:
    """Server-side authorized data scope for this caller (§12.2). Selected assets only.
    `pii_access` (an agent manifest's policy) can only tighten the workspace policy."""
    workspace = get_workspace(session, workspace_id)  # held, so require_role's lookup hits the identity map
    role = require_role(session, user, workspace_id, minimum_role)
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
    assets_by_source, cols_by_asset = _selected_assets(session, [s.id for s in sources])
    tags_by_column: dict[str, set[str]] = {}
    for source in sources:
        scope.source_ids.append(source.id)
        scope.source_dialects[source.id] = source_dialect(source.kind, source.execution_mode)
        for asset_id, fq in assets_by_source.get(source.id, []):
            scope.assets.append(fq)
            scope.asset_sources[fq] = source.id
            cols = cols_by_asset.get(asset_id, [])
            scope.columns[fq] = [name for name, _ in cols]
            for name, col_tags in cols:
                col_fq = f"{fq}.{name}"
                tags = set(col_tags or [])
                tags_by_column[col_fq] = tags
                restricted = "restricted" in tags or any(_matches(p, col_fq) for p in policy.restricted_columns)
                pii = "pii" in tags or any(_matches(p, col_fq) for p in policy.pii_columns)
                if restricted or (pii and not pii_cleared):
                    scope.denied_columns.append(col_fq)
    if policy.attribute_rules:
        apply_attribute_rules(scope, user.attributes, policy.attribute_rules, tags_by_column)
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
