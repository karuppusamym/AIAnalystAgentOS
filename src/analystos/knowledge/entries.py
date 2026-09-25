"""Glossary-style entries over the knowledge packs, for the readers that used to query
`context_entry ... OR workspace_id IS NULL` (compiler, lexicon, crawler glossary links, MCP
resources, the context API).

A workspace's own `context_entry` rows are unchanged; platform-wide knowledge now comes from the
read-only platform pack (and a workspace's other packs) through the index. Both are returned as
`EntryView`s so callers need not care which store a term came from.

The `analystos` frontmatter mapping is this platform's OKF producer extension (§4.1 permits
producer keys): `kind`, `synonyms`, `mapped_columns`, `domain_pack`, `origin`, `trusted`.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.knowledge import okf

# legacy context_entry kind <-> OKF `type` (types are producer-chosen, §4.1)
KIND_TYPES = {"term": "Glossary Term", "definition": "Definition", "metric": "Metric", "rule": "Business Rule",
              "note": "Note", "dashboard": "Dashboard", "table": "Table"}
TYPE_KINDS = {**{v.lower(): k for k, v in KIND_TYPES.items()}, "term": "term", "rule": "rule",
              "atlas business concept": "term", "concept": "term", "business concept": "term", "kpi": "metric"}
KIND_HEADINGS = {"term": "Definition", "definition": "Definition", "metric": "Definition", "rule": "Rule"}


def doc_kind(frontmatter: dict[str, Any]) -> str:
    ext = frontmatter.get("analystos")
    if isinstance(ext, dict) and isinstance(ext.get("kind"), str) and ext["kind"]:
        return ext["kind"]
    return TYPE_KINDS.get(str(frontmatter.get("type") or "").strip().lower(), "document")


def slugify(text: str, limit: int = 80) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return s or "entry"


def first_sentence(text: str, limit: int = 240) -> str | None:
    flat = " ".join(text.split())
    if not flat:
        return None
    m = re.search(r"(?<=[.!?])\s", flat)
    s = flat[: m.start()] if m else flat
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def render_entry(*, kind: str, name: str, body: str, synonyms: Iterable[str] = (), mapped_columns: Iterable[str] = (),
                 origin: str = "user", trusted: bool = True, domain_pack: str | None = None,
                 generated_by: str = "process:analystos") -> str:
    """One legacy-style entry as an OKF v0.2 concept document."""
    ext: dict[str, Any] = {"kind": kind, "origin": origin, "trusted": bool(trusted)}
    if synonyms:
        ext["synonyms"] = list(synonyms)
    if mapped_columns:
        ext["mapped_columns"] = list(mapped_columns)
    if domain_pack:
        ext["domain_pack"] = domain_pack
    fm: dict[str, Any] = {"type": KIND_TYPES.get(kind, kind.title()), "title": name, "analystos": ext,
                          "status": "stable" if trusted else "draft", "generated": {"by": generated_by},
                          "tags": sorted({kind, *([f"domain-{domain_pack}"] if domain_pack else [])})}
    desc = first_sentence(body)
    if desc:
        fm["description"] = desc
    heading = KIND_HEADINGS.get(kind)
    text = f"# {heading}\n\n{body.strip()}" if heading and not body.lstrip().startswith("#") else body
    return okf.render_document(fm, text)


@dataclass(frozen=True)
class EntryView:
    id: str
    kind: str
    name: str
    body: str
    synonyms: tuple[str, ...] = ()
    mapped_columns: tuple[str, ...] = ()
    origin: str = "user"
    trusted: bool = True
    workspace_id: str | None = None
    pack_id: str | None = None
    path: str | None = None
    sha256: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_global(self) -> bool:
        return self.pack_id is not None and self.workspace_id is None

    def as_dict(self) -> dict[str, Any]:
        out = {"id": self.id, "kind": self.kind, "name": self.name, "body": self.body, "synonyms": list(self.synonyms),
               "mapped_columns": list(self.mapped_columns), "origin": self.origin, "global": self.is_global}
        if self.path:
            out.update({"pack_id": self.pack_id, "path": self.path, "sha256": self.sha256})
        return out


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def doc_entry(d: Any, pack_slug: str) -> EntryView:
    fm = d.frontmatter or {}
    ext = fm.get("analystos") if isinstance(fm.get("analystos"), dict) else {}
    sections = okf.split_sections(d.body or "", fm)
    body = "\n\n".join(s.text for s in sections if s.text) or (d.description or "")
    trusted = ext.get("trusted") if isinstance(ext.get("trusted"), bool) else d.status == "stable"
    origin = str(ext.get("origin") or (f"pack:{ext['domain_pack']}" if ext.get("domain_pack") else f"okf:{pack_slug}"))
    return EntryView(id=d.id, kind=d.kind, name=d.title, body=body, synonyms=_strings(ext.get("synonyms")),
                     mapped_columns=_strings(ext.get("mapped_columns")), origin=origin, trusted=bool(trusted),
                     workspace_id=d.workspace_id, pack_id=d.pack_id, path=d.path, sha256=d.sha256,
                     extra={"type": d.type, "status": d.status, "trust_tier": d.trust_tier, "pack": pack_slug})


def pack_entries(session: Session, workspace_id: str, *, kinds: Iterable[str] | None = None,
                 exclude_kinds: Iterable[str] = (), trusted_only: bool = False) -> list[EntryView]:
    """Indexed documents of the packs this workspace may see (platform + its own), as entries."""
    from analystos.db.models import KnowledgeDocument
    from analystos.knowledge.store import visible_packs

    packs = {p.id: p.slug for p in visible_packs(session, workspace_id)}
    if not packs:
        return []
    q = select(KnowledgeDocument).where(KnowledgeDocument.pack_id.in_(list(packs)))
    if kinds is not None:
        q = q.where(KnowledgeDocument.kind.in_(list(kinds)))
    exclude = list(exclude_kinds)
    if exclude:
        q = q.where(KnowledgeDocument.kind.notin_(exclude))
    out = [doc_entry(d, packs[d.pack_id]) for d in session.scalars(q.order_by(KnowledgeDocument.path, KnowledgeDocument.id))]
    return [e for e in out if e.trusted] if trusted_only else out


def workspace_rows(session: Session, workspace_id: str, *, kinds: Iterable[str] | None = None,
                   exclude_kinds: Iterable[str] = (), trusted_only: bool = False) -> list[EntryView]:
    from analystos.db.models import ContextEntry

    q = select(ContextEntry).where(ContextEntry.workspace_id == workspace_id)
    if kinds is not None:
        q = q.where(ContextEntry.kind.in_(list(kinds)))
    exclude = list(exclude_kinds)
    if exclude:
        q = q.where(ContextEntry.kind.notin_(exclude))
    if trusted_only:
        q = q.where(ContextEntry.trusted.is_(True))
    return [EntryView(id=e.id, kind=e.kind, name=e.name, body=e.body, synonyms=tuple(e.synonyms or ()),
                      mapped_columns=tuple(e.mapped_columns or ()), origin=e.origin or "user", trusted=bool(e.trusted),
                      workspace_id=e.workspace_id)
            for e in session.scalars(q.order_by(ContextEntry.created_at.desc(), ContextEntry.id))]


def visible_entries(session: Session, workspace_id: str, *, kinds: Iterable[str] | None = None,
                    exclude_kinds: Iterable[str] = (), trusted_only: bool = False) -> list[EntryView]:
    kinds = list(kinds) if kinds is not None else None
    return (workspace_rows(session, workspace_id, kinds=kinds, exclude_kinds=exclude_kinds, trusted_only=trusted_only)
            + pack_entries(session, workspace_id, kinds=kinds, exclude_kinds=exclude_kinds, trusted_only=trusted_only))


def get_entry(session: Session, workspace_id: str, entry_id: str) -> EntryView | None:
    """One entry by id, only if this workspace may see it."""
    from analystos.db.models import ContextEntry, KnowledgeDocument
    from analystos.knowledge.store import visible_packs

    e = session.get(ContextEntry, entry_id)
    if e is not None:
        if e.workspace_id != workspace_id:
            return None
        return EntryView(id=e.id, kind=e.kind, name=e.name, body=e.body, synonyms=tuple(e.synonyms or ()),
                         mapped_columns=tuple(e.mapped_columns or ()), origin=e.origin or "user", trusted=bool(e.trusted),
                         workspace_id=e.workspace_id)
    d = session.get(KnowledgeDocument, entry_id)
    if d is None:
        return None
    packs = {p.id: p.slug for p in visible_packs(session, workspace_id)}
    return doc_entry(d, packs[d.pack_id]) if d.pack_id in packs else None
