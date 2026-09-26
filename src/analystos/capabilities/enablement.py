"""Per-workspace enablement and certification gating (spec v3 §3.1, P4-X01).

* **Disabled by default.** A capability runs in a workspace only when enabled there. An explicit
  row (owner or admin) always wins. Without one the default is *on* for the built-in set
  `playbook.investigate` needs (the playbook, its agents and their capabilities and tools), for the
  built-in tools (the platform's own toolbelt, which already has a global enabled flag), and for
  what an explicitly enabled playbook or agent binds (enabling `playbook.data_dictionary` enables
  its agent and that agent's lookups); *off* for everything else: other playbooks, pack,
  entry-point and non-investigate agents, methods and skills.
* **Governed elsewhere.** Connectors are governed by source registration and their evidence-derived
  certification, knowledge packs by the workspace policy `domain_packs`, MCP tools by the server
  allowlist and classification. Their enablement here is always on.
* **Certification.** Only `certified` capabilities run under autonomy >= 3 or on a schedule/alert.
"""
from __future__ import annotations

import fnmatch
import threading
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.capabilities.playbook import DEFAULT_PLAYBOOK
from analystos.contracts.capability import CapabilityManifest
from analystos.core.errors import InvalidInput

SELF_GOVERNED_KINDS = ("Connector", "Engine", "KnowledgePack")  # engines: governed by the gateway
AUTONOMOUS_ORIGINS = ("schedule", "alert")


def self_governed(m: CapabilityManifest) -> bool:
    return m.kind in SELF_GOVERNED_KINDS or m.source.startswith("mcp:")


def closure(snapshot: Any, root: str) -> set[str]:
    """A capability and everything it binds: playbook steps/expansions -> agents -> capabilities and tools."""
    out: set[str] = set()
    todo = [root]
    tool_ids = {m.spec.get("tool_id"): m.id for m in snapshot.list("Tool") if m.spec.get("tool_id")}
    while todo:
        cid = todo.pop()
        m = snapshot.manifests.get(cid)
        if m is None or cid in out:
            continue
        out.add(cid)
        if m.kind == "Playbook":
            for step in m.spec.get("steps") or []:
                todo += [step.get("use")] + [e.get("use") for e in step.get("expands") or []]
        elif m.kind == "Agent":
            for ref in m.spec.get("capabilities") or []:
                todo += [i for i in snapshot.manifests if fnmatch.fnmatchcase(i, ref)]
            todo += [tool_ids[t] for t in m.spec.get("tools") or [] if t in tool_ids]
        elif m.spec.get("tool") in tool_ids:
            todo.append(tool_ids[m.spec["tool"]])
        todo += [r for r in m.requires if not r.startswith(("engine:", "profile:", "extra:"))]
    return out


_defaults: dict[str, frozenset[str]] = {}
_lock = threading.Lock()


def default_enabled(snapshot: Any) -> frozenset[str]:
    with _lock:
        cached = _defaults.get(snapshot.digest)
    if cached is not None:
        return cached
    ids = {i for i in closure(snapshot, DEFAULT_PLAYBOOK) if snapshot.manifests[i].source == "builtin"}
    ids |= {m.id for m in snapshot.list("Tool") if m.source == "builtin"}
    value = frozenset(ids)
    with _lock:
        _defaults[snapshot.digest] = value
    return value


def overrides(session: Session, workspace_id: str) -> dict[str, bool]:
    from analystos.db.models import WorkspaceCapability

    return {r.capability_id: r.enabled for r in session.scalars(
        select(WorkspaceCapability).where(WorkspaceCapability.workspace_id == workspace_id))}


def _enabled_closure(snapshot: Any, explicit: dict[str, bool]) -> frozenset[str]:
    roots = frozenset(i for i, on in explicit.items() if on and i in snapshot.manifests)
    key = f"{snapshot.digest}:{','.join(sorted(roots))}"
    with _lock:
        cached = _defaults.get(key)
    if cached is None:
        cached = frozenset().union(*(closure(snapshot, r) for r in roots)) if roots else frozenset()
        with _lock:
            _defaults[key] = cached
    return cached


def is_enabled(m: CapabilityManifest, snapshot: Any, explicit: dict[str, bool]) -> bool:
    if self_governed(m):
        return True
    if m.id in explicit:
        return explicit[m.id]
    return m.id in default_enabled(snapshot) or m.id in _enabled_closure(snapshot, explicit)


def enabled_map(session: Session, workspace_id: str, snapshot: Any) -> dict[str, bool]:
    explicit = overrides(session, workspace_id)
    return {m.id: is_enabled(m, snapshot, explicit) for m in snapshot.list()}


def tool_enabled(session: Session, workspace_id: str, tool_id: str) -> bool:
    """Tool gate hook: a built-in tool is on unless the workspace turned its capability off."""
    from analystos.db.models import WorkspaceCapability

    row = session.scalar(select(WorkspaceCapability.enabled).where(
        WorkspaceCapability.workspace_id == workspace_id,
        WorkspaceCapability.capability_id == "tool." + tool_id.replace(".", "_")))
    return row is None or bool(row)


def autonomous(run: Any) -> bool:
    return (run.autonomy_level or 0) >= 3 or (run.origin or {}).get("type") in AUTONOMOUS_ORIGINS


def usable(m: CapabilityManifest, snapshot: Any, explicit: dict[str, bool], *, autonomous_run: bool) -> str | None:
    """None when the capability may run here, else the reason it may not."""
    from analystos.capabilities.registry import install_reason

    if missing := install_reason(m):
        return f"capability {m.ref} is unavailable on this installation: {missing}"
    if not is_enabled(m, snapshot, explicit):
        return f"capability {m.ref} is disabled for this workspace"
    if m.certification.status == "deprecated":
        return f"capability {m.ref} is deprecated"
    if autonomous_run and not m.autonomous_ok:
        return (f"capability {m.ref} is {m.certification.status}, not certified: autonomous runs (autonomy >= 3, "
                "schedules, alerts) use certified capabilities only")
    return None


def set_enabled(session: Session, user: Any, workspace_id: str, capability_id: str, enabled: bool, snapshot: Any) -> dict:
    """Workspace owners (and platform admins) enable or disable a capability for their workspace."""
    from analystos.db.models import WorkspaceCapability
    from analystos.governance.audit import audit
    from analystos.governance.policy import require_role

    if not getattr(user, "is_admin", False):
        require_role(session, user, workspace_id, "owner")
    m = snapshot.get(capability_id)
    if self_governed(m):
        raise InvalidInput(f"{m.kind} capabilities are governed by their own registration, not by enablement")
    row = session.scalar(select(WorkspaceCapability).where(WorkspaceCapability.workspace_id == workspace_id,
                                                            WorkspaceCapability.capability_id == capability_id))
    if row is None:
        row = WorkspaceCapability(workspace_id=workspace_id, capability_id=capability_id, enabled=enabled, updated_by=user.id)
        session.add(row)
    else:
        row.enabled, row.updated_by = enabled, user.id
    audit(f"user:{user.id}", "capability.enabled" if enabled else "capability.disabled", target=capability_id,
          workspace_id=workspace_id, details={"ref": m.ref}, session=session)
    return {"capability_id": capability_id, "ref": m.ref, "enabled": enabled}
