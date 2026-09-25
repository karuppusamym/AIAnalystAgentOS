"""Knowledge packs and their revisions: the system of record (P4-K01, spec v3 §6.1).

A pack is a content-addressed, versioned OKF v0.2 bundle held in the control plane: file bytes in
`knowledge_object` (keyed by sha256 per pack), and every change a new immutable
`knowledge_revision` with author, reason and the full {path: sha256} map. The Postgres index
(`knowledge/index.py`) is derived from the head revision and can be dropped and rebuilt at any time.

Scopes: exactly one read-only `platform` pack (no workspace; replaces the old NULL-workspace
`context_entry` rows), one `workspace` pack per workspace, and read-only `imported` packs (Atlas
or other OKF bundles), which change only by re-import. Read-only is enforced here: only a system
writer (seed, migration, importer) may commit to one.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id
from analystos.db.models import KnowledgeObject, KnowledgePack, KnowledgeRevision
from analystos.knowledge import okf

PLATFORM_SLUG = "platform"
WORKSPACE_SLUG = "workspace"
KINDS = ("platform", "workspace", "imported")


# ------------------------------------------------------------------------------------ packs
def _new_pack(**kw: Any) -> KnowledgePack:
    return KnowledgePack(id=new_id("kpk"), okf_version=okf.OKF_VERSION, okf_spec_revision=okf.OKF_SPEC_REVISION,
                         okf_spec_sha256=okf.OKF_SPEC_SHA256, **kw)


def platform_pack(session: Session, *, create: bool = True) -> KnowledgePack | None:
    pack = session.scalar(select(KnowledgePack).where(KnowledgePack.kind == "platform"))
    if pack is None and create:
        pack = _new_pack(workspace_id=None, kind="platform", slug=PLATFORM_SLUG, title="Platform knowledge",
                         read_only=True)
        session.add(pack)
        session.flush()
    return pack


def workspace_pack(session: Session, workspace_id: str, *, create: bool = True) -> KnowledgePack | None:
    pack = session.scalar(select(KnowledgePack).where(KnowledgePack.workspace_id == workspace_id,
                                                      KnowledgePack.kind == "workspace"))
    if pack is None and create:
        pack = _new_pack(workspace_id=workspace_id, kind="workspace", slug=WORKSPACE_SLUG, title="Workspace knowledge")
        session.add(pack)
        session.flush()
    return pack


def imported_pack(session: Session, workspace_id: str, slug: str, *, okf_root: str = "", title: str = "",
                  origin: dict[str, Any] | None = None) -> KnowledgePack:
    if okf.check_path(slug) or "/" in slug or slug in (WORKSPACE_SLUG, PLATFORM_SLUG):
        raise InvalidInput(f"invalid imported pack slug {slug!r}")
    pack = session.scalar(select(KnowledgePack).where(KnowledgePack.workspace_id == workspace_id, KnowledgePack.slug == slug))
    if pack is not None and pack.kind != "imported":
        raise InvalidInput(f"pack {slug!r} is not an imported pack")
    if pack is None:
        pack = _new_pack(workspace_id=workspace_id, kind="imported", slug=slug, title=title or slug, read_only=True,
                         okf_root=okf_root, origin=dict(origin or {}))
        session.add(pack)
        session.flush()
    else:
        pack.okf_root = okf_root
        if origin:
            pack.origin = dict(origin)
    return pack


def get_pack(session: Session, pack_id: str, workspace_id: str | None = None) -> KnowledgePack:
    """A pack the caller's workspace may see (its own or the platform pack); NotFound otherwise, so
    one workspace cannot learn that another's pack exists."""
    pack = session.get(KnowledgePack, pack_id)
    if pack is None or (workspace_id is not None and pack.workspace_id not in (workspace_id, None)):
        raise NotFound(f"knowledge pack {pack_id} not found")
    return pack


def visible_packs(session: Session, workspace_id: str) -> list[KnowledgePack]:
    """The platform pack plus the workspace's own packs. Never another workspace's."""
    return list(session.scalars(select(KnowledgePack).where(
        or_(KnowledgePack.workspace_id == workspace_id, KnowledgePack.kind == "platform")).order_by(KnowledgePack.id)))


# ------------------------------------------------------------------------------------ revisions
def head(session: Session, pack: KnowledgePack) -> KnowledgeRevision | None:
    if pack.head_revision is None:
        return None
    return session.scalar(select(KnowledgeRevision).where(KnowledgeRevision.pack_id == pack.id,
                                                          KnowledgeRevision.number == pack.head_revision))


def revision(session: Session, pack: KnowledgePack, number: int) -> KnowledgeRevision:
    rev = session.scalar(select(KnowledgeRevision).where(KnowledgeRevision.pack_id == pack.id,
                                                         KnowledgeRevision.number == number))
    if rev is None:
        raise NotFound(f"revision {number} of pack {pack.slug} not found")
    return rev


def revision_files(session: Session, pack: KnowledgePack, number: int | None = None) -> dict[str, bytes]:
    """Every file of a revision (default: head), path -> bytes."""
    rev = head(session, pack) if number is None else revision(session, pack, number)
    if rev is None:
        return {}
    shas = set(rev.files.values())
    blobs = {o.sha256: bytes(o.content) for o in session.scalars(
        select(KnowledgeObject).where(KnowledgeObject.pack_id == pack.id, KnowledgeObject.sha256.in_(shas)))}
    missing = shas - set(blobs)
    if missing:
        raise NotFound(f"pack {pack.slug} revision {rev.number} is missing {len(missing)} object(s)")
    return {path: blobs[sha] for path, sha in sorted(rev.files.items())}


def commit(session: Session, pack: KnowledgePack, files: Mapping[str, bytes], *, author: str, reason: str, origin: str,
           system: bool = False, merge: bool = False, deletes: tuple[str, ...] = (), meta: dict[str, Any] | None = None,
           index: bool = True, limits: okf.Limits = okf.LIMITS) -> KnowledgeRevision | None:
    """Write a new revision: `files` replaces the whole pack, or with `merge=True` is laid over the
    head (minus `deletes`). Returns None when the content is unchanged. Read-only packs accept only
    system writers. The index is refreshed for this pack unless `index=False`."""
    if pack.read_only and not system:
        raise Forbidden(f"knowledge pack {pack.slug} is read-only")
    content: dict[str, bytes] = dict(revision_files(session, pack)) if merge else {}
    for path in deletes:
        content.pop(path, None)
    content.update(files)
    total = 0
    for path, data in content.items():
        code = okf.check_path(path, limits)
        if code:
            raise InvalidInput(f"{code}: {path!r}")
        if len(data) > limits.max_document_bytes:
            raise InvalidInput(f"DOCUMENT_TOO_LARGE: {path} is {len(data)} bytes (limit {limits.max_document_bytes})")
        total += len(data)
    if len(content) > limits.max_files or total > limits.max_bundle_bytes:
        raise InvalidInput(f"BUNDLE_TOO_LARGE: {len(content)} files, {total} bytes")
    shas = {path: hashlib.sha256(data).hexdigest() for path, data in content.items()}
    digest = okf.content_digest(shas)
    current = head(session, pack)
    if current is not None and current.content_digest == digest:
        return None
    known = set(session.scalars(select(KnowledgeObject.sha256).where(KnowledgeObject.pack_id == pack.id,
                                                                     KnowledgeObject.sha256.in_(set(shas.values())))))
    for path, data in content.items():
        sha = shas[path]
        if sha not in known:
            session.add(KnowledgeObject(pack_id=pack.id, sha256=sha, size=len(data), content=data))
            known.add(sha)
    problems = okf.check_conformance(content, pack.okf_root, limits)
    number = (pack.head_revision or 0) + 1
    rev = KnowledgeRevision(id=new_id("krev"), pack_id=pack.id, number=number, parent_number=pack.head_revision,
                            author=author, reason=reason, origin=origin, files=dict(sorted(shas.items())),
                            content_digest=digest,
                            meta={**(meta or {}), "conformance": {"status": okf.CONFORMANCE_STATUS,
                                                                 "problems": [p.as_dict() for p in problems[:200]],
                                                                 "problem_count": len(problems)}})
    session.add(rev)
    pack.head_revision = number
    session.flush()
    if pack.workspace_id:
        from analystos.events.bus import emit

        emit(pack.workspace_id, "knowledge.revision_committed",
             {"pack_id": pack.id, "slug": pack.slug, "revision": number, "origin": origin, "files": len(content),
              "content_digest": digest}, actor=author, session=session)
    if index:
        from analystos.knowledge.index import index_pack

        index_pack(session, pack)
    return rev


def history(session: Session, pack: KnowledgePack, limit: int = 50) -> list[dict[str, Any]]:
    return [{"number": r.number, "author": r.author, "reason": r.reason, "origin": r.origin, "files": len(r.files),
             "content_digest": r.content_digest, "created_at": r.created_at}
            for r in session.scalars(select(KnowledgeRevision).where(KnowledgeRevision.pack_id == pack.id)
                                     .order_by(KnowledgeRevision.number.desc()).limit(limit))]
