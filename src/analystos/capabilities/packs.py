"""Domain packs (spec v3 §3.6): domain knowledge as data, read by domain-neutral core code.

A pack is a `kind: KnowledgePack` capability manifest (`packs/<name>/pack.yaml`, or an entry point).
Its `spec` holds the pack's data inline or as paths relative to the pack directory (format: packs/README.md):

  applies_when  {tables: [regex], columns: [regex]}: the pack auto-enables for a run whose selected
                tables or columns fully match one of them (case-insensitive)
  hints         naming vocabulary the deterministic skills merge: acronyms, key and display columns,
                event-start words, lifecycle pairs, domain keywords, person nouns (PII)
  templates     hypothesis templates, AnalysisSpec patterns over column slots (skills/hypothesis_templates)
  kpis          starter KPIs over the same outcomes
  knowledge     OKF-like markdown documents: YAML frontmatter + body (glossary terms, metrics, rules)
  benchmark     dataset generator, planted effects and null controls (tests/benchmarks)

Two scopes. *Hints* come from every installed pack: they only add vocabulary, and the crawler needs
them before any workspace has enabled anything. *Templates and knowledge* come from the packs a run
enables: the workspace policy's `domain_packs` list, or (when it is unset) every pack whose
`applies_when` matches the run's selected catalog.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from analystos.capabilities import registry
from analystos.contracts.capability import CapabilityManifest
from analystos.core.logging import get_logger

_log = get_logger(__name__)
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass(frozen=True)
class KnowledgeDoc:
    """One OKF-like document: a glossary term, metric or business rule."""

    kind: str
    name: str
    body: str
    synonyms: tuple[str, ...] = ()
    maps_to: tuple[str, ...] = ()
    path: str | None = None


@dataclass(frozen=True)
class DomainPack:
    id: str
    version: str
    summary: str
    root: Path | None
    applies_when: dict[str, Any]
    hints: dict[str, Any]
    templates: dict[str, Any]
    kpis: list[dict[str, Any]]
    knowledge: tuple[KnowledgeDoc, ...]
    benchmark: dict[str, Any] | None
    source: str = "builtin"

    @property
    def name(self) -> str:
        return self.id.split(".", 1)[1]

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def applies_to(self, tables: Iterable[str], columns: Iterable[str] = ()) -> bool:
        tables, columns = [t.lower() for t in tables], [c.lower() for c in columns]
        for key, names in (("tables", tables), ("columns", columns)):
            for pattern in self.applies_when.get(key) or []:
                rx = re.compile(pattern, re.I)
                if any(rx.fullmatch(n) for n in names):
                    return True
        return False


@dataclass(frozen=True)
class Hints:
    """Merged vocabulary of every installed pack. Every field only ever adds to a core default."""

    acronyms: dict[str, str] = field(default_factory=dict)
    key_columns: tuple[str, ...] = ()
    display_columns: tuple[str, ...] = ()
    event_start: tuple[str, ...] = ()
    lifecycle_pairs: tuple[tuple[str, str], ...] = ()
    domain_keywords: dict[str, frozenset[str]] = field(default_factory=dict)
    person_nouns: frozenset[str] = frozenset()


# ------------------------------------------------------------------------------------ loading
def _data(root: Path | None, value: Any, default: Any) -> Any:
    """A spec value is inline data or a path (file or directory) relative to the pack root."""
    if value is None:
        return default
    if isinstance(value, str):
        if root is None:
            raise ValueError(f"{value!r} is a path but the pack has no directory")
        path = (root / value).resolve()
        if root.resolve() not in path.parents and path != root.resolve():
            raise ValueError(f"{value!r} points outside the pack directory")
        if path.is_dir():
            return path
        return yaml.safe_load(path.read_text(encoding="utf-8")) or default
    return value


def parse_knowledge(text: str, path: str | None = None) -> KnowledgeDoc:
    m = _FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path or 'document'}: missing YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not meta.get("name") or meta.get("kind") not in ("term", "metric", "rule"):
        raise ValueError(f"{path or 'document'}: frontmatter needs name and kind (term | metric | rule)")
    return KnowledgeDoc(kind=meta["kind"], name=str(meta["name"]), body=m.group(2).strip(),
                        synonyms=tuple(meta.get("synonyms") or ()), maps_to=tuple(meta.get("maps_to") or ()), path=path)


def _knowledge(root: Path | None, value: Any) -> tuple[KnowledgeDoc, ...]:
    loaded = _data(root, value, [])
    if isinstance(loaded, Path):
        return tuple(parse_knowledge(p.read_text(encoding="utf-8"), str(p.relative_to(root)))
                     for p in sorted(loaded.rglob("*.md")))
    return tuple(KnowledgeDoc(kind=d["kind"], name=d["name"], body=d.get("body", ""), synonyms=tuple(d.get("synonyms") or ()),
                              maps_to=tuple(d.get("maps_to") or ())) for d in loaded)


def from_manifest(m: CapabilityManifest, packs_dir: Path | None = None) -> DomainPack:
    root = None
    if m.source.startswith("pack:"):
        root = (packs_dir or registry.PACKS_DIR) / m.source.split(":", 1)[1]
    s = m.spec
    kpis = _data(root, s.get("kpis"), [])
    return DomainPack(id=m.id, version=m.version, summary=m.summary, root=root, applies_when=dict(s.get("applies_when") or {}),
                      hints=dict(_data(root, s.get("hints"), {})), templates=dict(_data(root, s.get("templates"), {})),
                      kpis=list(kpis.get("kpis", []) if isinstance(kpis, dict) else kpis),
                      knowledge=_knowledge(root, s.get("knowledge")), benchmark=_data(root, s.get("benchmark"), None),
                      source=m.source)


def load_packs(packs_dir: Path | None = None, *, entry_points: bool = True) -> list[DomainPack]:
    """Every installed pack, sorted by id. A pack whose data does not load is skipped and logged:
    a broken pack must not take the crawler or a run down."""
    snap = registry.load(packs_dir=packs_dir or registry.PACKS_DIR, entry_points=entry_points, legacy=False,
                         connectors=False, strict=False)
    for p in snap.problems:
        _log.warning("capability problem while loading packs: %s", p)
    out = []
    for m in snap.list("KnowledgePack"):
        try:
            out.append(from_manifest(m, packs_dir))
        except (OSError, ValueError, yaml.YAMLError, KeyError, TypeError) as exc:
            _log.warning("domain pack %s skipped: %s", m.id, exc)
    return out


_lock = threading.Lock()
_installed: list[DomainPack] | None = None
_hints: Hints | None = None


def installed() -> list[DomainPack]:
    global _installed
    with _lock:
        if _installed is None:
            _installed = load_packs()
        return _installed


def reset() -> None:
    """Forget the cached packs (tests, hot reload)."""
    global _installed, _hints
    with _lock:
        _installed, _hints = None, None


def merge_hints(packs: Iterable[DomainPack]) -> Hints:
    acr: dict[str, str] = {}
    keys: list[str] = []
    display: list[str] = []
    starts: list[str] = []
    pairs: list[tuple[str, str]] = []
    domains: dict[str, set[str]] = {}
    nouns: set[str] = set()
    for p in packs:
        h = p.hints
        acr.update({str(k).lower(): str(v) for k, v in (h.get("acronyms") or {}).items()})
        keys += [k for k in h.get("key_columns") or [] if k not in keys]
        display += [k for k in h.get("display_columns") or [] if k not in display]
        starts += [k for k in h.get("event_start") or [] if k not in starts]
        pairs += [(a, b) for a, b in h.get("lifecycle_pairs") or [] if (a, b) not in pairs]
        for dom, kws in (h.get("domain_keywords") or {}).items():
            domains.setdefault(dom, set()).update(str(k).lower() for k in kws)
        nouns.update(str(n).lower() for n in h.get("person_nouns") or [])
    return Hints(acronyms=acr, key_columns=tuple(keys), display_columns=tuple(display), event_start=tuple(starts),
                 lifecycle_pairs=tuple(pairs), domain_keywords={d: frozenset(k) for d, k in domains.items()},
                 person_nouns=frozenset(nouns))


def hints() -> Hints:
    global _hints
    packs = installed()
    with _lock:
        if _hints is None:
            _hints = merge_hints(packs)
        return _hints


# ------------------------------------------------------------------------------------ enablement
def enabled_for(tables: Iterable[str], columns: Iterable[str] = (), explicit: list[str] | None = None,
                available: list[DomainPack] | None = None) -> list[DomainPack]:
    """The packs a run uses. `explicit` (workspace policy `domain_packs`) wins when set, even when
    empty; otherwise every pack whose `applies_when` matches the selected tables or columns."""
    packs = installed() if available is None else available
    if explicit is not None:
        wanted = {e if e.startswith("pack.") else f"pack.{e}" for e in explicit}
        return [p for p in packs if p.id in wanted]
    tables, columns = list(tables), list(columns)
    return [p for p in packs if p.applies_to(tables, columns)]


def for_scope(scope: Any, policy: Any = None) -> list[DomainPack]:
    """Packs enabled for a run's authorized scope and workspace policy."""
    tables = [a.split(".", 1)[-1] for a in scope.assets]
    columns = [c for cols in scope.columns.values() for c in cols]
    return enabled_for(tables, columns, getattr(policy, "domain_packs", None))
