"""The knowledge index (P4-K01): Postgres tables derived from pack head revisions, rebuildable at
any time with `analystos knowledge reindex`.

* `knowledge_document` — one row per OKF concept document (frontmatter, trust tier, staleness);
* `knowledge_section` — its top-level sections, each with a generated `tsvector` (GIN) and an
  embedding (pgvector **HNSW**, cosine);
* `knowledge_link` — the link graph (§6.1), resolved against the same revision.

Identity: document and section ids are derived from (pack, path, anchor), rows are written in
path order, and every ranking breaks ties on stable keys, so dropping the index and rebuilding it
from the same revisions gives identical retrieval results (tests/integration/test_knowledge_pack.py).

Retrieval is hybrid and deterministic: a lexical leg (Postgres full text, `ts_rank_cd` over an
OR-query of the question's words) and a vector leg (HNSW nearest neighbours), fused by reciprocal
rank (k=60). The lexical leg is Postgres full-text ranking, not Okapi BM25 — see
docs/10-architecture/okf-profile.md. Scope is always the packs a workspace may see.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from analystos.core.logging import get_logger
from analystos.db.models import (
    KnowledgeDocument,
    KnowledgeIndexState,
    KnowledgeLink,
    KnowledgePack,
    KnowledgeSection,
)
from analystos.knowledge import embeddings as emb
from analystos.knowledge import okf
from analystos.knowledge.entries import doc_kind

log = get_logger(__name__)

RRF_K = 60
HNSW_INDEX = "ix_knowledge_section_embedding_hnsw"
EMBEDDING_KEY = "embedding"
_WORD = re.compile(r"[a-z0-9]+")


def doc_id(pack_id: str, path: str) -> str:
    return "kdoc_" + hashlib.sha256(f"{pack_id}\0{path}".encode()).hexdigest()[:24]


def section_id(document_id: str, anchor: str) -> str:
    return "ksec_" + hashlib.sha256(f"{document_id}\0{anchor}".encode()).hexdigest()[:24]


# ------------------------------------------------------------------------------------ state
def _state(session: Session, key: str) -> dict[str, Any] | None:
    row = session.get(KnowledgeIndexState, key)
    return dict(row.value) if row is not None else None


def _set_state(session: Session, key: str, value: dict[str, Any]) -> None:
    row = session.get(KnowledgeIndexState, key)
    if row is None:
        session.add(KnowledgeIndexState(key=key, value=value))
    else:
        row.value = value
    session.flush()


def index_embedding(session: Session) -> dict[str, Any] | None:
    return _state(session, EMBEDDING_KEY)


def _provider_for_writes(session: Session) -> emb.EmbeddingProvider:
    """The provider the index already uses (so new packs join the same vector space); the
    configured one when the index is empty."""
    state = index_embedding(session)
    if state:
        p = emb.from_state(state)
        if p is not None:
            return p
    p = emb.configured()
    _use_provider(session, p)
    return p


def _column_dim(session: Session) -> int | None:
    return session.scalar(text("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'knowledge_section'::regclass "
                               "AND attname = 'embedding'"))


def _use_provider(session: Session, p: emb.EmbeddingProvider) -> None:
    """Retype the embedding column (and rebuild its HNSW index) when the dimension changes."""
    if _column_dim(session) != p.dim:
        session.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX}"))
        session.execute(text(f"ALTER TABLE knowledge_section ALTER COLUMN embedding TYPE vector({int(p.dim)}) USING NULL"))
        session.execute(text(f"CREATE INDEX {HNSW_INDEX} ON knowledge_section USING hnsw (embedding vector_cosine_ops)"))
    _set_state(session, EMBEDDING_KEY, emb.describe(p))


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


# ------------------------------------------------------------------------------------ build
_INSERT_SECTION = text("""
INSERT INTO knowledge_section (id, document_id, pack_id, workspace_id, ordinal, anchor, heading, text, sha256,
                               search_text, embedding, embedding_model)
VALUES (:id, :document_id, :pack_id, :workspace_id, :ordinal, :anchor, :heading, :text, :sha256,
        :search_text, CAST(:embedding AS vector), :embedding_model)
""")


def _search_text(doc: okf.OkfDocument, s: okf.Section) -> str:
    ext = doc.extension
    parts = [doc.title, " ".join(str(x) for x in ext.get("synonyms") or [] if isinstance(x, str)), s.heading, s.text]
    if s.ordinal == 0 and doc.description:
        parts.insert(1, doc.description)
    return "\n".join(p for p in parts if p)


def drop_pack(session: Session, pack_id: str) -> None:
    session.execute(delete(KnowledgeLink).where(KnowledgeLink.pack_id == pack_id))
    session.execute(delete(KnowledgeSection).where(KnowledgeSection.pack_id == pack_id))
    session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.pack_id == pack_id))
    session.execute(delete(KnowledgeIndexState).where(KnowledgeIndexState.key == f"pack:{pack_id}"))


def index_pack(session: Session, pack: KnowledgePack, provider: emb.EmbeddingProvider | None = None) -> dict[str, Any]:
    """(Re)build this pack's rows from its head revision."""
    from analystos.knowledge.store import head, revision_files

    provider = provider or _provider_for_writes(session)
    drop_pack(session, pack.id)
    rev = head(session, pack)
    files = revision_files(session, pack) if rev else {}
    problems: list[dict[str, str]] = []
    docs: list[okf.OkfDocument] = []
    for path in sorted(files):
        if not okf.is_markdown(path) or path.rsplit("/", 1)[-1] in okf.RESERVED:
            continue
        if pack.okf_root and not path.startswith(pack.okf_root.rstrip("/") + "/"):
            continue
        try:
            docs.append(okf.parse_document(path, files[path], root=pack.okf_root))
        except okf.OkfError as exc:
            problems.append({"code": exc.code, "path": path})
    sections: list[dict[str, Any]] = []
    links: list[KnowledgeLink] = []
    for d in docs:
        did = doc_id(pack.id, d.path)
        session.add(KnowledgeDocument(
            id=did, pack_id=pack.id, workspace_id=pack.workspace_id, revision=rev.number if rev else 0, path=d.path,
            concept_id=d.concept_id, type=d.type[:120], kind=doc_kind(d.frontmatter)[:40], title=d.title[:500],
            description=d.description, status=d.status[:20], trust_tier=d.trust_tier,
            stale_after=okf.parse_instant(d.frontmatter.get("stale_after")),
            frontmatter=_jsonable(d.frontmatter), body=d.body, sha256=d.sha256, size=d.size))
        for s in d.sections:
            sections.append({"id": section_id(did, s.anchor), "document_id": did, "pack_id": pack.id,
                             "workspace_id": pack.workspace_id, "ordinal": s.ordinal, "anchor": s.anchor[:200],
                             "heading": s.heading[:500], "text": s.text,
                             "sha256": hashlib.sha256(s.text.encode()).hexdigest(), "search_text": _search_text(d, s)})
        for link in d.links:
            links.append(KnowledgeLink(pack_id=pack.id, workspace_id=pack.workspace_id, source_path=d.path,
                                       raw=link.raw[:2000], kind=link.kind, target_path=link.target,
                                       resolved=bool(link.target and link.target in files)))
    session.flush()
    if sections:
        vectors = provider.embed([s["search_text"] for s in sections])
        model = emb.provider_id(provider)
        session.execute(_INSERT_SECTION, [{**s, "embedding": _vec(v), "embedding_model": model}
                                          for s, v in zip(sections, vectors, strict=True)])
    session.add_all(links)
    report = {"pack_id": pack.id, "slug": pack.slug, "revision": rev.number if rev else None,
              "content_digest": rev.content_digest if rev else None, "documents": len(docs), "sections": len(sections),
              "links": len(links), "dangling_links": sum(1 for x in links if x.kind == "internal" and not x.resolved),
              "problems": problems[:200], "embedding": emb.provider_id(provider)}
    _set_state(session, f"pack:{pack.id}", report)
    return report


def _jsonable(value: Any) -> Any:
    """YAML gives dates and datetimes; the JSON column wants strings."""
    import datetime as dt

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def drop_index(session: Session) -> None:
    """Delete every derived row. The packs and their revisions are untouched."""
    session.execute(text("DELETE FROM knowledge_link"))
    session.execute(text("DELETE FROM knowledge_section"))
    session.execute(text("DELETE FROM knowledge_document"))
    session.execute(text("DELETE FROM knowledge_index_state"))


def reindex(session: Session, *, provider: emb.EmbeddingProvider | None = None) -> dict[str, Any]:
    """Drop and rebuild the whole index from the packs' head revisions. Keeps the index's current
    embedding provider unless one is given (switching provider is `reembed`)."""
    current = index_embedding(session)
    provider = provider or (emb.from_state(current) if current else None) or emb.configured()
    drop_index(session)
    _use_provider(session, provider)
    reports = [index_pack(session, p, provider)
               for p in session.scalars(select(KnowledgePack).order_by(KnowledgePack.id))]
    session.flush()
    return {"packs": reports, "embedding": emb.describe(provider),
            "documents": sum(r["documents"] for r in reports), "sections": sum(r["sections"] for r in reports)}


def refresh(session: Session) -> list[dict[str, Any]]:
    """Index every pack whose indexed revision is not its head (after a migration or a crash)."""
    out = []
    for p in session.scalars(select(KnowledgePack).order_by(KnowledgePack.id)):
        state = _state(session, f"pack:{p.id}")
        if state is None or state.get("revision") != p.head_revision:
            out.append(index_pack(session, p))
    return out


def reembed(session: Session, provider: emb.EmbeddingProvider | None = None, *, batch: int = 256) -> dict[str, Any]:
    """The re-embed job (P4-K10): move every section to `provider` (default: the configured one),
    retyping the vector column and rebuilding the HNSW index when the dimension changes."""
    provider = provider or emb.configured()
    before = index_embedding(session)
    _use_provider(session, provider)
    model = emb.provider_id(provider)
    ids = list(session.scalars(select(KnowledgeSection.id).order_by(KnowledgeSection.id)))
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        rows = session.execute(select(KnowledgeSection.id, KnowledgeSection.search_text)
                               .where(KnowledgeSection.id.in_(chunk)).order_by(KnowledgeSection.id)).all()
        vectors = provider.embed([r.search_text for r in rows])
        session.execute(text("UPDATE knowledge_section SET embedding = CAST(:e AS vector), embedding_model = :m WHERE id = :id"),
                        [{"id": r.id, "e": _vec(v), "m": model} for r, v in zip(rows, vectors, strict=True)])
    session.flush()
    return {"from": before, "to": emb.describe(provider), "sections": len(ids)}


# ------------------------------------------------------------------------------------ retrieval
@dataclass(frozen=True)
class Hit:
    section_id: str
    document_id: str
    pack_id: str
    pack_slug: str
    pack_kind: str
    path: str
    concept_id: str
    type: str
    kind: str
    title: str
    anchor: str
    heading: str
    text: str
    document_sha256: str
    section_sha256: str
    status: str
    trust_tier: str
    score: float
    lexical_rank: int | None
    vector_rank: int | None
    similarity: float | None

    def receipt(self) -> dict[str, Any]:
        """What a caller cites: path, anchor, sha256 (spec v3 §4.3)."""
        return {"id": self.document_id, "pack": self.pack_slug, "path": self.path, "anchor": self.anchor,
                "sha256": self.document_sha256, "section_sha256": self.section_sha256}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def query_terms(query: str) -> list[str]:
    return sorted({w for w in _WORD.findall(query.lower()) if len(w) >= 2})


def _lexical(session: Session, packs: list[str], words: list[str], candidates: int) -> list[str]:
    if not words:
        return []
    rows = session.execute(text("""
        SELECT s.id, ts_rank_cd(s.tsv, q, 1) AS r
        FROM knowledge_section s, to_tsquery('english', :tsq) q
        WHERE s.pack_id = ANY(:packs) AND s.tsv @@ q
        ORDER BY r DESC, s.id LIMIT :n"""), {"tsq": " | ".join(words), "packs": packs, "n": candidates}).all()
    return [r.id for r in rows]


def _vector(session: Session, packs: list[str], query: str, candidates: int) -> tuple[list[str], dict[str, float]]:
    provider = emb.from_state(index_embedding(session))
    if provider is None:
        return [], {}
    qv = _vec(provider.embed([query])[0])
    session.execute(text("SELECT set_config('hnsw.ef_search', :v, true), set_config('hnsw.iterative_scan', 'relaxed_order', true)"),
                    {"v": str(max(64, min(1000, candidates * 4)))})
    rows = session.execute(text("""
        SELECT s.id, s.embedding <=> CAST(:q AS vector) AS d
        FROM knowledge_section s
        WHERE s.pack_id = ANY(:packs) AND s.embedding IS NOT NULL
        ORDER BY s.embedding <=> CAST(:q AS vector) LIMIT :n"""), {"q": qv, "packs": packs, "n": candidates}).all()
    ranked = sorted((round(float(r.d), 6), r.id) for r in rows)  # relaxed order: sort exactly, ties by id
    return [i for _, i in ranked], {i: round(1.0 - d, 6) for d, i in ranked}


def retrieve(session: Session, workspace_id: str, query: str, *, limit: int = 8, kinds: Iterable[str] | None = None,
             types: Iterable[str] | None = None, pack_ids: Iterable[str] | None = None, candidates: int = 50,
             exclude_kinds: Iterable[str] = ()) -> list[Hit]:
    """Hybrid retrieval over the packs this workspace may see (platform + its own). `pack_ids` can
    only narrow that set. The API the context compiler calls (P4-K05 builds on it)."""
    from analystos.knowledge.store import visible_packs

    visible = {p.id: p for p in visible_packs(session, workspace_id)}
    wanted = [p for p in visible if pack_ids is None or p in set(pack_ids)]
    if not wanted or not query.strip():
        return []
    lex = _lexical(session, wanted, query_terms(query), candidates)
    vec, sims = _vector(session, wanted, query, candidates)
    fused: dict[str, float] = {}
    for ranks in (lex, vec):
        for n, sid in enumerate(ranks, start=1):
            fused[sid] = fused.get(sid, 0.0) + 1.0 / (RRF_K + n)
    if not fused:
        return []
    rows = session.execute(select(KnowledgeSection, KnowledgeDocument)
                           .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeSection.document_id)
                           .where(KnowledgeSection.id.in_(list(fused)))).all()
    kinds_set = set(kinds) if kinds is not None else None
    types_set = {t.lower() for t in types} if types is not None else None
    excluded = set(exclude_kinds)
    lex_rank = {sid: n for n, sid in enumerate(lex, start=1)}
    vec_rank = {sid: n for n, sid in enumerate(vec, start=1)}
    hits = []
    for s, d in rows:
        if (kinds_set is not None and d.kind not in kinds_set) or d.kind in excluded:
            continue
        if types_set is not None and d.type.lower() not in types_set:
            continue
        p = visible[d.pack_id]
        hits.append(Hit(section_id=s.id, document_id=d.id, pack_id=p.id, pack_slug=p.slug, pack_kind=p.kind, path=d.path,
                        concept_id=d.concept_id, type=d.type, kind=d.kind, title=d.title, anchor=s.anchor,
                        heading=s.heading, text=s.text, document_sha256=d.sha256, section_sha256=s.sha256,
                        status=d.status, trust_tier=d.trust_tier, score=round(fused[s.id], 8),
                        lexical_rank=lex_rank.get(s.id), vector_rank=vec_rank.get(s.id), similarity=sims.get(s.id)))
    hits.sort(key=lambda h: (-h.score, h.path, h.anchor))
    return hits[:limit]


def signature(hits: Iterable[Hit]) -> list[tuple[Any, ...]]:
    """What "identical retrieval results" compares: ids, order, scores and ranks."""
    return [(h.section_id, h.path, h.anchor, h.score, h.lexical_rank, h.vector_rank, h.similarity) for h in hits]


def stats(session: Session) -> dict[str, Any]:
    counts = {t: session.scalar(text(f"SELECT count(*) FROM {t}"))
              for t in ("knowledge_document", "knowledge_section", "knowledge_link")}
    hnsw = session.scalar(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'knowledge_section' "
                               "AND indexname = :n"), {"n": HNSW_INDEX})
    return {**counts, "hnsw_index": hnsw, "embedding": index_embedding(session)}
