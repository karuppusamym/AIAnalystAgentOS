"""Binding a run to exact capability versions (spec v3 §3.1, P4-X01).

When a plan materializes, the run records the playbook, every agent its steps use and everything
those agents bind (capabilities, tools) as full manifests plus their `id@version` refs. The refs go
into the plan hash, so an approval covers exact versions. The engine, dispatch and RunContext read
the bound manifests, so a registry reload never changes a run in flight. Runs planned before
bindings existed fall back to the current registry.
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

    def to_json(self) -> dict[str, Any]:
        return {"playbook": self.playbook.manifest.id, "refs": self.refs, "manifests": self.manifests, "skipped": self.skipped}


def requested_playbook(run: Any) -> str:
    return (run.capabilities or {}).get("playbook") or DEFAULT_PLAYBOOK


def bind_run(session: Session, run: Any, snapshot: registry.Snapshot | None = None) -> Binding:
    """Resolve and check what the run will use. A required step whose agent is disabled, deprecated or
    (for an autonomous run) not certified fails the plan with the reason; an optional one is skipped."""
    snap = snapshot or registry.current()
    explicit = enablement.overrides(session, run.workspace_id)
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
    return Binding(playbook=playbook, refs=snap.bind(ids),
                   manifests={i: snap.get(i).model_dump(mode="json") for i in sorted(ids)}, skipped=skipped)


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
