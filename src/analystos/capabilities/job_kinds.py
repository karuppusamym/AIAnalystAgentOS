"""Which job kinds can start in a workspace, and why not (workbench UX §1, spec v4 §15, P4-04).

**Start work** lists Explain, Compare, Forecast, Predict, Prepare data and Monitor. Each is available only
when everything it needs is true *now*, decided in code from the registry and the workspace:

1. an **executor** exists for its payload (an executable work-order type, or an implemented service);
2. the capabilities it runs are **registered and usable here** (installed, enabled for the workspace,
   not deprecated: `enablement.usable`), never inferred from the catalog listing a name;
3. the caller's **role** allows starting it;
4. the scope has **data** to run on.

Every failed condition is returned as a reason with a code and a remediation, so the UI shows why a
choice is disabled and never sends a request that would start a pretend run. A later row that adds an
executor (P5 ML, a `prepare` playbook) flips availability by registering it — no change here.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from analystos.contracts.brief import JobKindAvailability
from analystos.security.auth import role_at_least


@dataclass(frozen=True)
class JobKind:
    key: str
    label: str
    work_order_kind: str
    entry: dict[str, Any]
    capabilities: tuple[str, ...]  # patterns; at least one must be registered and usable (empty: service-backed)
    min_role: str
    checks: tuple[str, ...]  # required readiness checks
    advisory: tuple[str, ...] = field(default=())


COMMON = ("capability", "scope", "schema_drift")
JOB_KINDS: tuple[JobKind, ...] = (
    JobKind("explain", "Explain", "diagnose", {"type": "work_order", "payload_type": "analysis"},
            ("playbook.investigate",), "analyst", COMMON + ("grain", "key_uniqueness", "join_fanout", "missingness"),
            ("freshness", "coverage")),
    JobKind("compare", "Compare", "compare", {"type": "work_order", "payload_type": "analysis"},
            ("Method[segment]",), "analyst",
            COMMON + ("grain", "join_fanout", "missingness"), ("freshness", "key_uniqueness", "coverage")),
    JobKind("forecast", "Forecast", "forecast", {"type": "work_order", "payload_type": "ml"},
            ("playbook.forecast*", "method.forecast*"), "analyst",
            COMMON + ("freshness", "coverage", "missingness"), ("grain",)),
    JobKind("predict", "Predict", "predict", {"type": "work_order", "payload_type": "ml"},
            ("playbook.train*", "playbook.score*"), "analyst",
            COMMON + ("grain", "key_uniqueness", "label_availability", "coverage", "missingness"), ("freshness",)),
    JobKind("prepare", "Prepare data", "prepare", {"type": "recipe", "route": "/api/workspaces/{workspace_id}/recipes"},
            (), "editor", COMMON + ("grain", "key_uniqueness", "join_fanout"), ("freshness", "missingness")),
    JobKind("monitor", "Monitor", "monitor", {"type": "monitor", "route": "/api/workspaces/{workspace_id}/monitors"},
            (), "editor", COMMON, ("freshness",)),
)
BY_KEY = {k.key: k for k in JOB_KINDS}
# Work-order kinds (contracts.work.JobKind) that have no Start-work choice of their own map onto one.
WORK_ORDER_KIND = {"diagnose": "explain", "describe": "explain", "compare": "compare", "forecast": "forecast",
                   "predict": "predict", "prepare": "prepare", "monitor": "monitor", "experiment": "predict"}


def get(key: str) -> JobKind:
    from analystos.core.errors import InvalidInput

    k = BY_KEY.get(key) or BY_KEY.get(WORK_ORDER_KIND.get(key, ""))
    if k is None:
        raise InvalidInput(f"unknown job kind {key!r}; one of {', '.join(BY_KEY)}")
    return k


def _reason(code: str, message: str, remediation: str) -> dict[str, str]:
    return {"code": code, "message": message, "remediation": remediation}


def executor_reason(job: JobKind) -> dict[str, str] | None:
    """None when something can execute this job kind here."""
    from analystos.contracts.work import EXECUTABLE_TYPES

    if job.entry["type"] == "work_order" and job.entry["payload_type"] not in EXECUTABLE_TYPES:
        what = {"ml": "an MLSpec (governed ML, P5-01..06)", "pipeline": "a PipelineSpec"}.get(job.entry["payload_type"],
                                                                                            job.entry["payload_type"])
        return _reason("no_executor", f"{job.label} runs {what}, and no executor for it is installed on this platform yet",
                       "Choose a supported job kind, such as Explain or Compare, explicitly; nothing is started in its place.")
    return None


def _matching(snapshot: Any, pattern: str) -> list[Any]:
    """`Kind[tag]` selects every manifest of that kind carrying the tag (so a new segment method counts
    without a change here); anything else is an id glob."""
    if pattern.endswith("]") and "[" in pattern:
        kind, _, tag = pattern[:-1].partition("[")
        return [m for m in snapshot.list(kind) if tag in (m.tags or [])]
    return [m for m in snapshot.list() if fnmatch.fnmatchcase(m.id, pattern)]


def _method_reason(m: Any) -> str | None:
    """Analysis methods are the closed vocabulary every `AnalysisSpec` is validated against (methods/registry);
    runs use them without a per-workspace switch, so only installation and deprecation decide here."""
    from analystos.capabilities.registry import install_reason

    if missing := install_reason(m):
        return f"method {m.ref} is unavailable on this installation: {missing}"
    if m.certification.status == "deprecated":
        return f"method {m.ref} is deprecated"
    return None


def capability_state(job: JobKind, snapshot: Any, explicit: dict[str, bool]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """The candidate capabilities with their usability, and the reasons when none is usable."""
    from analystos.capabilities.enablement import usable

    if not job.capabilities:
        return [], []
    found = [m for pattern in job.capabilities for m in _matching(snapshot, pattern)]
    if not found:
        return [], [_reason("not_registered", f"no capability for {job.label} is registered ({', '.join(job.capabilities)})",
                            "Install or enable a pack that provides it; an administrator reloads the capability registry.")]
    caps, reasons = [], []
    for m in found:
        why = usable(m, snapshot, explicit, autonomous_run=False) if m.kind != "Method" else _method_reason(m)
        caps.append({"id": m.id, "ref": m.ref, "kind": m.kind, "usable": why is None, "reason": why,
                     "certification": m.certification.status})
        if why is not None:
            reasons.append(_reason("capability_unusable", why,
                                   "A workspace owner enables it (Settings > Capabilities), or an administrator installs "
                                   "the missing profile or extra."))
    if any(c["usable"] for c in caps):
        return caps, []
    return caps, reasons


def availability(session: Session, user: Any, workspace_id: str, *, snapshot: Any = None) -> list[dict[str, Any]]:
    """Every Start-work job kind for this caller in this workspace, with its reasons."""
    from analystos.capabilities import enablement, registry
    from analystos.governance.policy import member_role, require_role, resolve_scope

    require_role(session, user, workspace_id, "viewer")
    snap = snapshot or registry.current()
    explicit = enablement.overrides(session, workspace_id)
    role = "owner" if getattr(user, "is_admin", False) else (member_role(session, user, workspace_id) or "viewer")
    try:
        assets = resolve_scope(session, user, workspace_id, minimum_role="viewer").assets
    except Exception:  # noqa: BLE001 - an unresolvable scope is a reason, not a crash
        assets = []
    out = []
    for job in JOB_KINDS:
        reasons: list[dict[str, str]] = []
        if (r := executor_reason(job)) is not None:
            reasons.append(r)
        caps, cap_reasons = capability_state(job, snap, explicit)
        reasons += cap_reasons
        if not role_at_least(role, job.min_role):
            reasons.append(_reason("role", f"{job.label} needs the {job.min_role} role here; you are {role}",
                                   "Ask a workspace owner for the role."))
        if not assets:
            reasons.append(_reason("no_data", "no selected, ready table is in your scope",
                                   "Add a source, discover it and select its tables (Data > Sources)."))
        entry = {**job.entry, **({"route": job.entry["route"].format(workspace_id=workspace_id)} if "route" in job.entry
                                 else {"route": f"/api/workspaces/{workspace_id}/work-orders"})}
        out.append(JobKindAvailability(key=job.key, label=job.label, work_order_kind=job.work_order_kind,  # type: ignore[arg-type]
                                       available=not reasons, reasons=reasons, capabilities=caps, entry=entry,
                                       readiness_checks=list(job.checks), min_role=job.min_role).model_dump(mode="json"))
    return out
