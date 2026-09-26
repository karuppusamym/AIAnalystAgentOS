"""Binding a run to exact capability versions (spec v3 §3.1, P4-X01; ADR-0021, P7-03).

When a plan materializes, the run records the playbook, every agent its steps use and everything
those agents bind (capabilities, tools) as full manifests plus their `id@version` refs. The refs go
into the plan hash, so an approval covers exact versions. The engine, dispatch and RunContext read
the bound manifests, so a registry reload never changes a run in flight. Runs planned before
bindings existed fall back to the current registry.

The binding also records what the run is an instance of and what it measured with (ADR-0021): the
definition ref with its content hash (a built-in playbook: manifest version plus digest), the
semantic model and metric versions, and the method versions. A pinned run (a schedule fire, a work
order) arrives with `capabilities.pinned`: its manifests replace the current registry's for this run,
its metric versions replace the currently approved ones, and its frozen AnalysisSpec set is replayed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from analystos.capabilities import enablement, registry
from analystos.capabilities.playbook import DEFAULT_PLAYBOOK, Playbook, evaluate, parse
from analystos.contracts.capability import CapabilityManifest
from analystos.core.errors import PolicyDenied


@dataclass
class Binding:
    playbook: Playbook
    refs: list[str]
    manifests: dict[str, dict[str, Any]]
    skipped: dict[str, str] = field(default_factory=dict)
    definition: dict[str, Any] = field(default_factory=dict)
    semantic: dict[str, Any] = field(default_factory=dict)
    methods: list[str] = field(default_factory=list)
    pinned: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out = {"playbook": self.playbook.manifest.id, "refs": self.refs, "manifests": self.manifests, "skipped": self.skipped,
               "definition": self.definition, "semantic": self.semantic, "methods": self.methods}
        if self.pinned:
            out["pinned"] = self.pinned
        if self.warnings:
            out["warnings"] = self.warnings
        return out


def requested_playbook(run: Any) -> str:
    return (run.capabilities or {}).get("playbook") or DEFAULT_PLAYBOOK


def semantic_versions(session: Session, workspace_id: str) -> dict[str, Any]:
    """The semantic model version and approved metric versions current in the workspace now."""
    from analystos.semantic.service import approved_metrics, current_model

    model = current_model(session, workspace_id)
    return {"model": {"id": model.id, "version": model.version, "content_hash": model.content_hash} if model else None,
            "metrics": {name: {"id": m.id, "version": m.version, "content_hash": m.content_hash}
                        for name, m in sorted(approved_metrics(session, workspace_id).items())}}


def _definition_manifest(run: Any) -> CapabilityManifest | None:
    """A workspace-published (or, in a dev workspace, draft) playbook definition the run was started from."""
    defn = (run.capabilities or {}).get("definition") or {}
    raw = defn.get("manifest")
    if not raw:
        return None
    m = CapabilityManifest.model_validate({**raw, "source": f"definition:{defn.get('id')}"})
    # Publishing is the workspace's enablement and certification of its own composition; the agents,
    # skills and tools it binds are still checked one by one (enablement and certification) below.
    status = "certified" if defn.get("status") in ("published", "deprecated") or defn.get("dev") else "draft"
    return m.model_copy(update={"certification": m.certification.model_copy(
        update={"status": status, "evidence": f"definition {defn.get('key')}@{defn.get('version')}"})})


def bind_run(session: Session, run: Any, snapshot: registry.Snapshot | None = None) -> Binding:
    """Resolve and check what the run will use. A required step whose agent is disabled, deprecated or
    (for an autonomous run) not certified fails the plan with the reason; an optional one is skipped."""
    from analystos.services.definitions import builtin_ref

    snap = snapshot or registry.current()
    caps = run.capabilities or {}
    pinned = dict(caps.get("pinned") or {})
    replace: dict[str, CapabilityManifest] = {}
    warnings: list[str] = []
    for cap_id, raw in (pinned.get("manifests") or {}).items():
        replace[cap_id] = CapabilityManifest.model_validate(raw)
        current = snap.manifests.get(cap_id)
        if current is not None and current.certification.status == "deprecated":
            warnings.append(f"pinned {replace[cap_id].ref} is deprecated in the installed registry ({current.ref})")
    defn_manifest = _definition_manifest(run)
    if defn_manifest is not None:
        replace[defn_manifest.id] = defn_manifest
    if replace:
        snap = registry.pinned(snap, replace.values())
    explicit = enablement.overrides(session, run.workspace_id)
    if (caps.get("definition") or {}).get("source") == "workspace":  # published in this workspace (or pinned from it)
        explicit.setdefault(requested_playbook(run), True)
    auto = enablement.autonomous(run)
    pb_manifest = snap.get(requested_playbook(run))
    if pb_manifest.kind != "Playbook":
        raise PolicyDenied(f"{pb_manifest.id} is not a playbook")
    problem = enablement.usable(pb_manifest, snap, explicit, autonomous_run=auto)
    if problem:
        raise PolicyDenied(problem)
    playbook = parse(pb_manifest)
    ids: set[str] = {pb_manifest.id}
    skipped: dict[str, str] = {}
    ns = {"run": {"autonomy_level": run.autonomy_level, "origin": run.origin or {}}}
    for step in playbook.body.steps:
        if step.when and not evaluate(step.when, ns):
            continue
        uses = [step.use] + [e.use for e in step.expands]
        reasons = [r for u in uses if (r := enablement.usable(snap.get(u), snap, explicit, autonomous_run=auto))]
        if reasons:
            if step.optional:
                skipped[step.key] = reasons[0]
                continue
            raise PolicyDenied(f"step {step.key}: {reasons[0]}")
        for u in uses:
            ids |= {i for i in enablement.closure(snap, u) if snap.manifests[i].kind != "Playbook"}
    definition = {k: v for k, v in (caps.get("definition") or {}).items() if k != "manifest"} or \
        builtin_ref(pb_manifest).model_dump()
    pinned.pop("manifests", None)  # they are the bound manifests now
    return Binding(playbook=playbook, refs=snap.bind(ids),
                   manifests={i: snap.get(i).model_dump(mode="json") for i in sorted(ids)}, skipped=skipped,
                   definition=definition,
                   semantic=pinned.get("semantic") or semantic_versions(session, run.workspace_id),
                   methods=list(pinned.get("methods") or sorted(m.ref for m in snap.list("Method"))),
                   pinned=pinned, warnings=warnings)


def bound_manifest(run: Any, cap_id: str) -> CapabilityManifest | None:
    raw = ((run.capabilities or {}).get("manifests") or {}).get(cap_id)
    return CapabilityManifest.model_validate(raw) if raw else None


def resolve(run: Any, cap_id: str) -> CapabilityManifest:
    """The version this run bound, else (a run planned before bindings) the current registry's."""
    return bound_manifest(run, cap_id) or registry.current().get(cap_id)


def run_playbook(run: Any) -> Playbook:
    return parse(resolve(run, requested_playbook(run)))


def bound_refs(run: Any) -> list[str]:
    return list((run.capabilities or {}).get("refs") or [])


def bound_metric_ids(run: Any) -> list[str] | None:
    """The semantic metric rows (ids) this run measures with, or None for a run bound before P7-03."""
    semantic = (run.capabilities or {}).get("semantic")
    if not isinstance(semantic, dict) or "metrics" not in semantic:
        return None
    return [m["id"] for m in (semantic.get("metrics") or {}).values() if isinstance(m, dict) and m.get("id")]
