"""Knowledge studio (P4-U04): browse and edit a workspace's OKF documents, see revision history, and
read the semantic graph that the UI draws.

Editing is a human act through the API: a save writes one workspace-pack revision authored by the
user. Platform and imported packs are read-only (`store.commit` refuses them). Trust fields are
editable within limits the server enforces rather than trusts:

* `verified` (§5.2) may lose entries but only ever gains one — the saving user's own
  `human:<id>` review, stamped by the server (`mark_reviewed`). Nobody can write another person's
  or a process's verification;
* `analystos.review` marks a document the review queue wrote and may replace. A human edit turns
  it into owner content: the marker moves to `analystos.reviewed_draft`, so no later draft can
  overwrite what a person edited (the K07 invariant, `suggestions.protected`);
* every save names the revision it was based on (`base_sha256` of the document, or none for a new
  path), so two editors cannot silently overwrite each other.

The graph joins governed facts (approved semantic model and metrics, validated or user-declared
table relationships, links and column mappings of human-reviewed documents) with inferred ones
(proposed metrics, discovered relationships, links of unreviewed documents, pending AI
suggestions). `governed` is the only styling signal the UI needs: solid versus dashed.
"""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound
from analystos.core.ids import utcnow
from analystos.db.models import (
    KnowledgeDocument,
    KnowledgeLink,
    KnowledgePack,
    KnowledgeRevision,
    KnowledgeSuggestion,
    Relationship,
    SourceAsset,
)
from analystos.knowledge import okf, store

REVIEW_KEY = "review"
REVIEWED_DRAFT_KEY = "reviewed_draft"
MAX_GRAPH_NODES = 400


# ------------------------------------------------------------------------------------ packs
def pack_out(session: Session, pack: KnowledgePack, *, can_edit: bool) -> dict[str, Any]:
    rev = store.head(session, pack)
    return {"id": pack.id, "kind": pack.kind, "slug": pack.slug, "title": pack.title, "read_only": pack.read_only,
            "writable": can_edit and pack.kind == "workspace" and not pack.read_only, "head_revision": pack.head_revision,
            "okf_root": pack.okf_root, "okf_version": pack.okf_version, "git_remote": pack.git_remote,
            "git_branch": pack.git_branch, "origin": pack.origin or {}, "files": len(rev.files) if rev else 0,
            "content_digest": rev.content_digest if rev else None, "updated_at": pack.updated_at}


def packs(session: Session, workspace_id: str, *, can_edit: bool) -> list[dict[str, Any]]:
    """The packs this workspace sees, its own writable pack first (created on first look)."""
    store.workspace_pack(session, workspace_id)
    order = {"workspace": 0, "imported": 1, "platform": 2}
    rows = sorted(store.visible_packs(session, workspace_id), key=lambda p: (order.get(p.kind, 9), p.slug))
    return [pack_out(session, p, can_edit=can_edit) for p in rows]


# ------------------------------------------------------------------------------------ documents
def _ext(meta: dict[str, Any]) -> dict[str, Any]:
    ext = meta.get("analystos")
    return ext if isinstance(ext, dict) else {}


def _authorship(meta: dict[str, Any]) -> str:
    """Who a document belongs to: `review_queue` (a draft the queue may replace) or `owner`."""
    return "review_queue" if isinstance(_ext(meta).get(REVIEW_KEY), dict) else "owner"


def _summary(pack: KnowledgePack, path: str, data: bytes) -> dict[str, Any]:
    from analystos.knowledge.index import doc_id

    out: dict[str, Any] = {"path": path, "document_id": doc_id(pack.id, path), "sha256": hashlib.sha256(data).hexdigest(),
                           "size": len(data), "markdown": okf.is_markdown(path),
                           "reserved": path.rsplit("/", 1)[-1] in okf.RESERVED}
    if not out["markdown"] or out["reserved"]:
        return {**out, "type": None, "title": path.rsplit("/", 1)[-1]}
    try:
        meta, _ = okf.parse_frontmatter(path, okf.decode(path, data))
    except okf.OkfError as exc:
        return {**out, "type": None, "title": path.rsplit("/", 1)[-1], "problem": exc.code}
    meta = meta or {}
    ext = _ext(meta)
    return {**out, "type": str(meta.get("type") or "") or None, "title": str(meta.get("title") or path.rsplit("/", 1)[-1]),
            "status": str(meta.get("status") or "stable"), "trust_tier": okf.trust_tier(meta),
            "stale": okf.is_stale(meta, utcnow()), "kind": ext.get("kind"), "authorship": _authorship(meta),
            "tags": [str(t) for t in meta.get("tags") or []] if isinstance(meta.get("tags"), list) else []}


def documents(session: Session, pack: KnowledgePack, revision: int | None = None) -> dict[str, Any]:
    files = store.revision_files(session, pack, revision)
    return {"pack_id": pack.id, "revision": revision or pack.head_revision,
            "documents": [_summary(pack, p, d) for p, d in files.items()]}


def _trust(meta: dict[str, Any]) -> dict[str, Any]:
    stale_at = okf.parse_instant(meta.get("stale_after"))
    return {"tier": okf.trust_tier(meta), "verified": okf.verified_entries(meta), "status": str(meta.get("status") or "stable"),
            "stale_after": stale_at.isoformat() if stale_at else None, "stale": okf.is_stale(meta, utcnow()),
            "trusted": _ext(meta).get("trusted") if isinstance(_ext(meta).get("trusted"), bool) else None}


def read(session: Session, pack: KnowledgePack, path: str, revision: int | None = None) -> dict[str, Any]:
    files = store.revision_files(session, pack, revision)
    if path not in files:
        raise NotFound(f"{path} is not in pack {pack.slug}" + (f" revision {revision}" if revision else ""))
    data = files[path]
    out = {**_summary(pack, path, data), "pack_id": pack.id, "revision": revision or pack.head_revision,
           "text": data.decode("utf-8", errors="replace"), "frontmatter": None, "body": None, "trust": None,
           "sections": [], "links": []}
    if out["markdown"] and not out["reserved"]:
        try:
            doc = okf.parse_document(path, data, root=pack.okf_root)
        except okf.OkfError:
            return out
        out.update(frontmatter=doc.frontmatter, body=doc.body, trust=_trust(doc.frontmatter),
                   sections=[{"anchor": s.anchor, "heading": s.heading} for s in doc.sections],
                   links=[{"raw": link.raw, "kind": link.kind, "target": link.target, "exists": link.target in files}
                          for link in doc.links])
    return out


def locate(session: Session, workspace_id: str, document_id: str) -> dict[str, Any]:
    """A document id from a context receipt -> its pack and path (only packs this workspace sees)."""
    d = session.get(KnowledgeDocument, document_id)
    if d is None or d.workspace_id not in (workspace_id, None):
        raise NotFound(f"document {document_id} not found")
    return {"document_id": d.id, "pack_id": d.pack_id, "path": d.path, "title": d.title, "revision": d.revision}


def _entry_key(e: dict[str, Any]) -> tuple[str, str]:
    """(by, at) with `at` normalised: YAML reads an instant as a datetime, JSON sends it as text."""
    at = okf.parse_instant(e.get("at"))
    return str(e.get("by") or ""), at.isoformat() if at else str(e.get("at") or "")


def _checked_frontmatter(frontmatter: dict[str, Any], previous: dict[str, Any] | None, user_id: str,
                         mark_reviewed: bool) -> dict[str, Any]:
    fm = dict(frontmatter)
    if not str(fm.get("type") or "").strip():
        raise InvalidInput("frontmatter needs a non-empty `type` (OKF §4)")
    prev = previous or {}
    before = {_entry_key(e) for e in okf.verified_entries(prev)}
    kept = list(okf.verified_entries(fm))
    forged = [e for e in kept if _entry_key(e) not in before]
    if forged:
        raise InvalidInput("`verified` can only keep existing entries; use mark_reviewed to add your own review",
                           details={"entries": forged})
    if mark_reviewed:
        me = f"human:{user_id}"
        kept = [e for e in kept if str(e.get("by")) != me] + [{"by": me, "at": utcnow().replace(microsecond=0).isoformat()}]
    if kept:
        fm["verified"] = kept
    else:
        fm.pop("verified", None)
    ext = dict(_ext(fm))
    prev_review = _ext(prev).get(REVIEW_KEY)
    ext.pop(REVIEW_KEY, None)  # a human edit makes the document owner content (see module doc)
    if isinstance(prev_review, dict):
        ext[REVIEWED_DRAFT_KEY] = prev_review
    elif REVIEWED_DRAFT_KEY in _ext(prev):
        ext[REVIEWED_DRAFT_KEY] = _ext(prev)[REVIEWED_DRAFT_KEY]
    else:
        ext.pop(REVIEWED_DRAFT_KEY, None)
    if ext:
        fm["analystos"] = ext
    else:
        fm.pop("analystos", None)
    if "stale_after" in fm and fm["stale_after"] not in (None, "") and okf.parse_instant(fm["stale_after"]) is None:
        raise InvalidInput("`stale_after` must be an ISO 8601 instant (OKF §5.5)")
    if fm.get("stale_after") in (None, ""):
        fm.pop("stale_after", None)
    return fm


def _writable(pack: KnowledgePack) -> None:
    if pack.kind != "workspace" or pack.read_only:
        raise Forbidden(f"knowledge pack {pack.slug} is read-only ({pack.kind}); edit the workspace pack")


def _check_base(files: dict[str, bytes], path: str, base_sha256: str | None) -> bytes | None:
    current = files.get(path)
    if current is None and base_sha256:
        raise Conflict(f"{path} was deleted since you opened it", details={"path": path})
    if current is not None and hashlib.sha256(current).hexdigest() != (base_sha256 or ""):
        raise Conflict(f"{path} changed since you opened it; reload and apply your edit again",
                       details={"path": path, "current_sha256": hashlib.sha256(current).hexdigest()})
    return current


def save(session: Session, pack: KnowledgePack, user: Any, *, path: str, frontmatter: dict[str, Any], body: str,
         base_sha256: str | None, mark_reviewed: bool = False, reason: str | None = None) -> dict[str, Any]:
    """Write one document as a new workspace-pack revision (a human edit)."""
    from analystos.governance.audit import audit

    _writable(pack)
    code = okf.check_path(path)
    if code or not okf.is_markdown(path):
        raise InvalidInput(f"{code or 'NOT_MARKDOWN'}: {path!r}")
    if path.rsplit("/", 1)[-1] in okf.RESERVED:
        raise InvalidInput(f"{path} is a reserved OKF file (index/log); it is not edited here")
    files = store.revision_files(session, pack)
    current = _check_base(files, path, base_sha256)
    previous = None
    if current is not None:
        try:
            previous, _ = okf.parse_frontmatter(path, okf.decode(path, current))
        except okf.OkfError:
            previous = None
    fm = _checked_frontmatter(frontmatter, previous, user.id, mark_reviewed)
    data = okf.render_document(fm, body).encode()
    try:
        okf.parse_document(path, data, root=pack.okf_root)
    except okf.OkfError as exc:
        raise InvalidInput(f"the document does not parse as OKF: {exc}") from None
    rev = store.commit(session, pack, {path: data}, author=f"user:{user.id}", origin="studio", merge=True,
                       reason=(reason or "").strip()[:500] or f"{'edit' if current is not None else 'create'} {path}",
                       meta={"studio": {"path": path, "action": "edit" if current is not None else "create"}})
    audit(f"user:{user.id}", "knowledge.document.saved", workspace_id=pack.workspace_id, target=f"{pack.id}:{path}",
          decision="allow", details={"revision": pack.head_revision, "changed": rev is not None,
                                     "mark_reviewed": mark_reviewed}, session=session)
    return {"changed": rev is not None, "revision": pack.head_revision, "document": read(session, pack, path)}


def delete(session: Session, pack: KnowledgePack, user: Any, *, path: str, base_sha256: str,
           reason: str | None = None) -> dict[str, Any]:
    from analystos.governance.audit import audit

    _writable(pack)
    files = store.revision_files(session, pack)
    if path not in files:
        raise NotFound(f"{path} is not in pack {pack.slug}")
    _check_base(files, path, base_sha256)
    store.commit(session, pack, {}, author=f"user:{user.id}", origin="studio", merge=True, deletes=(path,),
                 reason=(reason or "").strip()[:500] or f"delete {path}", meta={"studio": {"path": path, "action": "delete"}})
    audit(f"user:{user.id}", "knowledge.document.deleted", workspace_id=pack.workspace_id, target=f"{pack.id}:{path}",
          decision="allow", details={"revision": pack.head_revision}, session=session)
    return {"revision": pack.head_revision, "deleted": path}


def revisions(session: Session, pack: KnowledgePack, *, path: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first, each with the paths it added, changed and removed against its parent. With
    `path`, only the revisions that touched that document."""
    rows = list(session.scalars(select(KnowledgeRevision).where(KnowledgeRevision.pack_id == pack.id)
                                .order_by(KnowledgeRevision.number.desc())))
    by_number = {r.number: r for r in rows}
    out: list[dict[str, Any]] = []
    for r in rows:
        parent = by_number.get(r.parent_number) if r.parent_number else None
        before = parent.files if parent else {}
        added = sorted(set(r.files) - set(before))
        removed = sorted(set(before) - set(r.files))
        changed = sorted(p for p in set(r.files) & set(before) if r.files[p] != before[p])
        if path and path not in added + removed + changed:
            continue
        out.append({"number": r.number, "parent": r.parent_number, "author": r.author, "reason": r.reason, "origin": r.origin,
                    "files": len(r.files), "content_digest": r.content_digest, "created_at": r.created_at,
                    "added": added[:200], "changed": changed[:200], "removed": removed[:200],
                    "sha256": r.files.get(path) if path else None,
                    "conformance_problems": ((r.meta or {}).get("conformance") or {}).get("problem_count", 0)})
        if len(out) >= max(1, min(limit, 500)):
            break
    return out


# ------------------------------------------------------------------------------------ graph
def graph(session: Session, workspace_id: str) -> dict[str, Any]:
    """Nodes (tables, datasets, metrics, documents, pending suggestions) and edges, each edge
    `governed` (solid) or inferred (dashed), with the reason it is one or the other."""
    from analystos.semantic import service as sem

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def node(nid: str, kind: str, label: str, **extra: Any) -> str:
        if nid not in nodes:
            nodes[nid] = {"id": nid, "kind": kind, "label": label, **extra}
        return nid

    def edge(a: str, b: str, kind: str, governed: bool, why: str, label: str = "") -> None:
        edges.append({"source": a, "target": b, "kind": kind, "governed": governed, "why": why, "label": label})

    assets = {a.id: a for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id))}
    by_name: dict[str, SourceAsset] = {}
    for a in sorted(assets.values(), key=lambda x: (not x.selected, x.id)):
        for key in (a.name.lower(), a.source_name.lower(), f"{a.schema_name}.{a.name}".lower()):
            by_name.setdefault(key, a)

    def table(a: SourceAsset) -> str:
        return node(f"table:{a.id}", "table", f"{a.schema_name}.{a.name}", selected=a.selected)

    def table_ref(ref: str) -> str | None:
        """`schema.table` or `source.table.column` -> a table node (a known asset when one matches)."""
        parts = [p for p in ref.lower().split(".") if p]
        for key in (".".join(parts[-3:-1]), parts[-2] if len(parts) >= 2 else "", ".".join(parts[-2:]), parts[-1] if parts else ""):
            if key and key in by_name:
                return table(by_name[key])
        return node(f"table:{'.'.join(parts[:-1]) or ref}", "table", ".".join(parts[:-1]) or ref) if len(parts) >= 2 else None

    for a in assets.values():
        if a.selected:
            table(a)
    for r in session.scalars(select(Relationship).where(Relationship.workspace_id == workspace_id)):
        a, b = assets.get(r.from_asset_id), assets.get(r.to_asset_id)
        if a is None or b is None:
            continue
        governed = bool(r.validated) or r.origin == "user"
        edge(table(a), table(b), "join", governed, "validated or declared by a person" if governed
             else f"discovered ({r.origin}, confidence {r.confidence:.2f})", f"{r.from_column} → {r.to_column}")

    model = sem.current_model(session, workspace_id)
    if model is not None:
        model_governed = model.status == "approved"
        why = f"semantic model v{model.version} {model.status}"
        for d in model.datasets or []:
            ds = node(f"dataset:{d['name']}", "dataset", d["name"], status=model.status)
            src = str(d.get("source") or "")
            if src and " " not in src.strip() and "." in src:
                t = table_ref(src + ".x")
                if t:
                    edge(ds, t, "source", model_governed, why)
        for r in model.relationships or []:
            a = node(f"dataset:{r.get('from')}", "dataset", str(r.get("from")))
            b = node(f"dataset:{r.get('to')}", "dataset", str(r.get("to")))
            edge(a, b, "relationship", model_governed, why, str(r.get("name") or ""))
    latest: dict[str, Any] = {}
    for m in sem.metric_rows(session, workspace_id):
        if m.status in ("rejected", "deprecated") and m.name in latest:
            continue
        latest[m.name] = m
    for m in latest.values():
        if m.status in ("rejected", "deprecated"):
            continue
        governed = m.status == "approved"
        mid = node(f"metric:{m.name}", "metric", m.display_name or m.name, status=m.status, version=m.version)
        defn = m.definition or {}
        why = f"metric v{m.version} {m.status}"
        if defn.get("dataset"):
            edge(mid, node(f"dataset:{defn['dataset']}", "dataset", str(defn["dataset"])), "measures", governed, why)
        for col in defn.get("source_columns") or []:
            t = table_ref(str(col))
            if t:
                edge(mid, t, "reads", governed, why, str(col).split(".")[-1])

    visible = {p.id: p for p in store.visible_packs(session, workspace_id)}
    docs = list(session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.pack_id.in_(list(visible)))))
    doc_by_path = {(d.pack_id, d.path): d for d in docs}

    def doc_node(d: KnowledgeDocument) -> str:
        p = visible[d.pack_id]
        return node(f"doc:{d.id}", "document", d.title, pack_id=d.pack_id, pack_kind=p.kind, path=d.path,
                    trust_tier=d.trust_tier, doc_kind=d.kind)

    for d in docs:
        if visible[d.pack_id].kind == "workspace":
            doc_node(d)
        cols = _ext(d.frontmatter or {}).get("mapped_columns") or []
        reviewed = d.trust_tier == "human-reviewed"
        for col in cols if isinstance(cols, list) else []:
            t = table_ref(str(col))
            if t:
                edge(doc_node(d), t, "maps", reviewed, f"document {d.trust_tier}", str(col).split(".")[-1])
    for link in session.scalars(select(KnowledgeLink).where(KnowledgeLink.pack_id.in_(list(visible)),
                                                            KnowledgeLink.kind == "internal", KnowledgeLink.resolved.is_(True))):
        a, b = doc_by_path.get((link.pack_id, link.source_path)), doc_by_path.get((link.pack_id, link.target_path or ""))
        if a is None or b is None or a.id == b.id:
            continue
        edge(doc_node(a), doc_node(b), "links", a.trust_tier == "human-reviewed", f"link in a {a.trust_tier} document")

    for s in session.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == workspace_id,
                                                                KnowledgeSuggestion.status == "pending")
                             .order_by(KnowledgeSuggestion.created_at).limit(100)):
        sid = node(f"suggestion:{s.id}", "suggestion", s.title, suggestion_id=s.id, confidence=s.confidence, doc_kind=s.kind)
        kind, _, ref = s.subject.partition(":")
        target = None
        if kind == "asset" and ref in assets:
            target = table(assets[ref])
        elif kind == "metric":
            target = f"metric:{ref.split('@', 1)[0]}" if f"metric:{ref.split('@', 1)[0]}" in nodes else None
        if target:
            edge(sid, target, "suggests", False, f"AI suggestion awaiting review (confidence {s.confidence:.2f})")

    ids = list(nodes)[:MAX_GRAPH_NODES]
    keep = set(ids)
    kept_edges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    return {"nodes": [nodes[i] for i in ids], "edges": kept_edges, "truncated": len(nodes) > MAX_GRAPH_NODES,
            "governed": sum(1 for e in kept_edges if e["governed"]), "inferred": sum(1 for e in kept_edges if not e["governed"])}
