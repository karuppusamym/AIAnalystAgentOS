"""Method registry: the closed analysis vocabulary, read from `kind: Method` capability manifests.

The vocabulary stays closed (a hypothesis may only use a registered method) while the registry is
open: built-in methods ship as `src/analystos/methods/<name>.py` + `<name>.yaml`, third-party methods
as a Python entry point in group `analystos.capabilities`. Directory packs are config-only, so a
`python:` method declared by a pack is refused (it would let data name code to import).

Names come from the manifests alone (no import), so validating a spec never imports a method module;
`get` imports and instantiates on first use and checks that the object satisfies the protocol.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from analystos.capabilities import registry as cap_registry
from analystos.contracts.capability import CapabilityManifest
from analystos.core.errors import InvalidInput
from analystos.core.logging import get_logger
from analystos.methods.base import Method

_log = get_logger(__name__)
DEFAULT_ORDER = 1000


@dataclass
class MethodRegistry:
    manifests: dict[str, CapabilityManifest]  # name -> manifest, in vocabulary order
    problems: list[str] = field(default_factory=list)
    _instances: dict[str, Method] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def names(self) -> list[str]:
        return list(self.manifests)

    def get(self, name: str) -> Method:
        m = self.manifests.get(name)
        if m is None:
            raise InvalidInput(f"unknown analysis method {name!r}; registered methods: {', '.join(self.manifests)}")
        with self._lock:
            if name not in self._instances:
                self._instances[name] = _instantiate(name, m)
            return self._instances[name]


def _instantiate(name: str, m: CapabilityManifest) -> Method:
    obj: Any = cap_registry.resolve_entry(m)
    if isinstance(obj, type):
        obj = obj()
    if not isinstance(obj, Method):
        raise InvalidInput(f"{m.id}: entry {m.entry} does not implement the Method protocol")
    if obj.name != name:
        raise InvalidInput(f"{m.id}: entry {m.entry} names itself {obj.name!r}, expected {name!r}")
    return obj


def build(extra: list[tuple[str, dict[str, Any]]] | None = None, *, entry_points: bool = True) -> MethodRegistry:
    """Every registered method manifest, ordered by `spec.order` then name. Non-strict: one broken
    plugin is reported and skipped, never takes the vocabulary down."""
    snap = cap_registry.load(packs_dir=cap_registry.PACKS_DIR, entry_points=entry_points, legacy=False, connectors=False,
                             strict=False, extra=extra or ())
    problems = list(snap.problems)
    chosen: list[CapabilityManifest] = []
    for m in snap.list("Method"):
        if m.source.startswith("pack:"):
            problems.append(f"{m.source}: {m.id} ignored: directory packs are config-only; ship a method as code")
            continue
        if not (m.entry or "").startswith("python:"):
            problems.append(f"{m.source}: {m.id} ignored: a method needs a python: entry")
            continue
        chosen.append(m)
    chosen.sort(key=lambda m: (int((m.spec or {}).get("order", DEFAULT_ORDER)), m.id))
    for p in problems:
        _log.warning("analysis method registry: %s", p)
    return MethodRegistry(manifests={m.id.split(".", 1)[1]: m for m in chosen}, problems=problems)


_lock = threading.Lock()
_current: MethodRegistry | None = None


def current() -> MethodRegistry:
    global _current
    with _lock:
        if _current is None:
            _current = build()
        return _current


def reset(registry: MethodRegistry | None = None) -> None:
    """Forget (or replace) the process-wide registry: hot reload and tests."""
    global _current
    with _lock:
        _current = registry
