"""Capability registry (ADR-0011): discover, validate and serve capability manifests.

Discovery order (later sources may not redefine an id a built-in already owns):
1. built-in manifests  `src/analystos/capabilities/builtin/*.yaml`
2. the legacy registries (tools, skills, agent YAML) bridged into manifests during the
   compatibility window, so everything that exists today is discoverable; and one Connector per
   source kind, generated from the kind catalog (connectors/kinds.py) with its certification
   derived from live evidence files (connectors/certification.py)
3. directory packs      `packs/<name>/**/*.yaml` (config-only)
4. Python entry points  group `analystos.capabilities` (a callable returning manifests or dicts)

Validation happens at load: a malformed manifest, a duplicate id or an unknown `requires`
reference fails the whole load with every problem listed. A reload builds a new immutable
snapshot and swaps it in; callers holding an old snapshot (runs in flight) keep their versions.
"""
from __future__ import annotations

import fnmatch
import importlib
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from analystos.contracts.capability import CapabilityManifest
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import stable_hash

BUILTIN_DIR = Path(__file__).parent / "builtin"
REPO_ROOT = Path(__file__).resolve().parents[3]
PACKS_DIR = REPO_ROOT / "packs"
ENTRY_POINT_GROUP = "analystos.capabilities"


class CapabilityLoadError(InvalidInput):
    code = "capability_load_failed"


@dataclass(frozen=True)
class Snapshot:
    """An immutable, versioned view of every loaded capability."""

    manifests: dict[str, CapabilityManifest]
    digest: str
    problems: tuple[str, ...] = field(default_factory=tuple)

    def get(self, cap_id: str) -> CapabilityManifest:
        m = self.manifests.get(cap_id)
        if m is None:
            raise NotFound(f"capability {cap_id} is not registered")
        return m

    def find(self, pattern: str, kind: str | None = None) -> list[CapabilityManifest]:
        """Glob over ids (`method.*`); sorted for stable plans."""
        return [m for i, m in sorted(self.manifests.items())
                if fnmatch.fnmatchcase(i, pattern) and (kind is None or m.kind == kind)]

    def list(self, kind: str | None = None) -> list[CapabilityManifest]:
        return [m for _, m in sorted(self.manifests.items()) if kind is None or m.kind == kind]

    def bind(self, ids: Iterable[str]) -> list[str]:
        """id@version refs a plan binds; part of the plan hash so an approval covers exact versions."""
        return sorted(self.get(i).ref for i in set(ids))


# ------------------------------------------------------------------------------------ loading
def _read_yaml(path: Path) -> list[dict[str, Any]]:
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
    out: list[dict[str, Any]] = []
    for d in docs:
        out.extend(d if isinstance(d, list) else [d])
    return out


def _parse(raw: dict[str, Any], source: str, problems: list[str]) -> CapabilityManifest | None:
    try:
        return CapabilityManifest.model_validate({**raw, "source": source})
    except ValidationError as exc:
        where = raw.get("id", "<no id>") if isinstance(raw, dict) else "<not a mapping>"
        problems.append(f"{source}: {where}: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]))
        return None


def _legacy_manifests() -> list[dict[str, Any]]:
    """Today's tools, skills and agents as manifests (compatibility window, ADR-0011 consequences)."""
    from analystos.skills.registry import SKILLS
    from analystos.tools.registry import TOOLS, load_agent_specs

    side = {"none": "none", "internal_write": "write_internal", "external_write": "write_external"}
    out: list[dict[str, Any]] = []
    for t in TOOLS.values():
        effect = side[t.side_effects]
        if effect == "none" and t.category in ("sql", "metadata", "dataframe", "data_quality", "statistics"):
            effect = "read_source"
        out.append({"kind": "Tool", "id": "tool." + t.tool_id.replace(".", "_"), "version": _semver(t.version),
                    "summary": t.name, "entry": f"builtin:{t.tool_id}", "input_schema": t.input_schema,
                    "output_schema": t.output_schema, "side_effect": effect,
                    "cost_class": {"low": "free", "medium": "query", "high": "compute"}[t.cost_profile],
                    "permissions": [f"role:{t.min_role}"], "certification": {"status": "certified", "evidence": "tests/unit"},
                    "tags": [t.category], "spec": {"tool_id": t.tool_id, "approval_policy": t.approval_policy,
                                                   "risk": t.risk, "description": t.description}})
    for s in SKILLS:
        out.append({"kind": "Skill", "id": "skill." + s["id"], "version": "1.0.0", "summary": s["description"][:200],
                    "entry": f"python:{s['function']}",
                    "determinism": "deterministic" if s.get("deterministic", True) else "model",
                    "side_effect": "read_source", "cost_class": "query",
                    "certification": {"status": "certified", "evidence": "tests/unit"}, "tags": [s["category"]]})
    for a in load_agent_specs():
        out.append({"kind": "Agent", "id": "agent." + a.id, "version": _semver(a.version), "summary": a.name,
                    "entry": f"builtin:agent:{a.id}", "determinism": "model", "side_effect": "write_internal",
                    "cost_class": "llm_small", "certification": {"status": "certified" if a.phase == "mvp" else "tested"},
                    "spec": {"description": a.description, "tools": a.tools, "skills": a.skills,
                             "model_profile": a.model_profile, "policies": a.policies.model_dump(), "phase": a.phase}})
    return out


def _connector_manifests() -> list[dict[str, Any]]:
    """One Connector manifest per source kind; the kind catalog stays the single list."""
    from analystos.connectors.certification import connector_manifests

    return connector_manifests()


def _semver(v: str) -> str:
    parts = str(v).split(".")
    return ".".join((parts + ["0", "0"])[:3])


def _entry_point_manifests(problems: list[str]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            produced = ep.load()()
            for item in produced if isinstance(produced, list) else [produced]:
                raw = item.model_dump() if isinstance(item, CapabilityManifest) else dict(item)
                out.append((f"entrypoint:{ep.dist.name if ep.dist else ep.name}", raw))
        except Exception as exc:  # a broken plugin must not take the platform down, but it is reported
            problems.append(f"entrypoint:{ep.name}: {type(exc).__name__}: {exc}")
    return out


def load(*, builtin_dir: Path = BUILTIN_DIR, packs_dir: Path | None = PACKS_DIR, entry_points: bool = True,
         legacy: bool = True, extra: Iterable[tuple[str, dict[str, Any]]] = (), strict: bool = True,
         connectors: bool = True) -> Snapshot:
    """Build a snapshot. `strict` raises on any problem; non-strict keeps valid manifests and reports problems."""
    problems: list[str] = []
    raws: list[tuple[str, dict[str, Any]]] = []
    if builtin_dir.is_dir():
        raws += [("builtin", r) for p in sorted(builtin_dir.glob("*.yaml")) for r in _read_yaml(p)]
    if legacy:
        raws += [("builtin", r) for r in _legacy_manifests()]
    if connectors:
        raws += [("builtin", r) for r in _connector_manifests()]
    if packs_dir is not None and packs_dir.is_dir():
        for pack in sorted(p for p in packs_dir.iterdir() if p.is_dir()):
            raws += [(f"pack:{pack.name}", r) for p in sorted(pack.rglob("*.yaml")) for r in _read_yaml(p)
                     if isinstance(r, dict) and r.get("apiVersion") == "analystos/v1"]
    if entry_points:
        raws += _entry_point_manifests(problems)
    raws += list(extra)

    manifests: dict[str, CapabilityManifest] = {}
    for source, raw in raws:
        m = _parse(raw, source, problems)
        if m is None:
            continue
        if m.id in manifests:
            problems.append(f"{source}: duplicate capability id {m.id} (already defined by {manifests[m.id].source})")
            continue
        manifests[m.id] = m
    for m in manifests.values():
        for req in m.requires:
            if req.startswith("engine:"):
                continue  # engine features are checked when an engine is bound (wave 4)
            if not any(fnmatch.fnmatchcase(i, req) for i in manifests):
                problems.append(f"{m.source}: {m.id} requires unknown capability {req}")
    if problems and strict:
        raise CapabilityLoadError("capability load failed:\n- " + "\n- ".join(problems))
    digest = stable_hash(sorted(m.ref for m in manifests.values()))
    return Snapshot(manifests=manifests, digest=digest, problems=tuple(problems))


# ------------------------------------------------------------------------------------ process-wide registry
_lock = threading.Lock()
_current: Snapshot | None = None


def current() -> Snapshot:
    global _current
    with _lock:
        if _current is None:
            _current = load()
        return _current


def reload(**kw: Any) -> Snapshot:
    """Hot reload: build first, swap only if valid, so a bad pack never replaces a good registry."""
    global _current
    fresh = load(**kw)
    with _lock:
        _current = fresh
    return fresh


def resolve_entry(manifest: CapabilityManifest) -> Callable[..., Any]:
    """The callable behind a `python:` entry. Other schemes are dispatched by their own runtimes."""
    if not manifest.entry or not manifest.entry.startswith("python:"):
        raise InvalidInput(f"{manifest.id} has no python entry ({manifest.entry})")
    target = manifest.entry[len("python:"):]
    module, _, attr = target.partition(":") if ":" in target else target.rpartition(".")
    obj: Any = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def overlay(base: Snapshot, manifests: Iterable[CapabilityManifest]) -> Snapshot:
    """A snapshot with extra, already-validated manifests layered on top (e.g. one workspace's MCP
    tools). An overlay can never replace a capability the base already defines."""
    merged = dict(base.manifests)
    problems = list(base.problems)
    for m in manifests:
        if m.id in merged:
            problems.append(f"{m.source}: duplicate capability id {m.id} (already defined by {merged[m.id].source})")
            continue
        merged[m.id] = m
    return Snapshot(manifests=merged, digest=stable_hash(sorted(m.ref for m in merged.values())), problems=tuple(problems))
