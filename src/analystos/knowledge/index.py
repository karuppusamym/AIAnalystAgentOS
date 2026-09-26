"""The knowledge index (P4-K01): Postgres tables derived from pack head revisions, rebuildable at
any time with `analystos knowledge reindex`.

* `knowledge_document` — one row per OKF concept document (frontmatter, trust tier, staleness);
* `knowledge_section` — its top-level sections, each with a generated `tsvector` (GIN) and an
  embedding (pgvector **HNSW**, cosine);
* `knowledge_link` — the link graph (§6.1), resolved against the same revision.

Identity: document and section ids are derived from (pack, path, anchor), rows are written in
path order, and every ranking breaks ties on stable keys, so dropping the index and rebuilding it
from the same revisions gives identical retrieval results (tests/integration/test_knowledge_pack.py).

Retrieval is hybrid and deterministic: a lexical leg (Okapi BM25 over the sections' `tsvector`
lexemes, with per-pack corpus statistics kept in the index state; P4-K05) and a vector leg (HNSW
nearest neighbours), fused by reciprocal rank (k=60), then optionally one hop over the link graph.
The P4-K01 lexical leg (`ts_rank_cd`) stays selectable so the retrieval benchmark can compare them.
Scope is always the packs a workspace may see.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import delete, event, select, text
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
BM25_K1 = 1.2
BM25_B = 0.75
HOP_DECAY = 0.25  # what a link passes on: a quarter of the linking hit's fused score
LEXICAL = ("bm25", "ts_rank_cd")
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
        # psycopg prepares repeated statements server-side; a plan prepared against the old column type
        # fails ("cached plan must not change result type") once the type changes, on this connection
        # and on every pooled one. Re-embed is an admin step, so drop the pool once the change commits.
        session.execute(text("DEALLOCATE ALL"))
        engine = session.get_bind().engine
        event.listen(session, "after_commit", lambda _s: engine.dispose(), once=True)
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
    session.flush()
    report = {"pack_id": pack.id, "slug": pack.slug, "revision": rev.number if rev else None,
              "content_digest": rev.content_digest if rev else None, "documents": len(docs), "sections": len(sections),
              "links": len(links), "dangling_links": sum(1 for x in links if x.kind == "internal" and not x.resolved),
              "problems": problems[:200], "embedding": emb.provider_id(provider), "bm25": _bm25_stats(session, pack.id)}
    _set_state(session, f"pack:{pack.id}", report)
    return report


def _tokens(tsv: str) -> str:
    """SQL for a section's length in tokens: its lexeme occurrences (tsvector positions)."""
    return f"(SELECT coalesce(sum(coalesce(array_length(u.positions, 1), 1)), 0) FROM unnest({tsv}) u)"


def _bm25_stats(session: Session, pack_id: str) -> dict[str, int]:
    """The corpus statistics BM25 needs, per pack: section count and total token count."""
    row = session.execute(text(f"SELECT count(*) AS n, coalesce(sum({_tokens('s.tsv')}), 0) AS tokens "
                               "FROM knowledge_section s WHERE s.pack_id = :p"), {"p": pack_id}).one()
    return {"sections": int(row.n), "tokens": int(row.tokens)}


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
    lexical_share: float | None = None  # share of the question's lexemes this section contains (BM25 leg)
    via: str | None = None  # one-hop hits: the path of the document that links here

    def receipt(self) -> dict[str, Any]:
        """What a caller cites: path, anchor, sha256 (spec v3 §4.3)."""
        return {"id": self.document_id, "pack": self.pack_slug, "path": self.path, "anchor": self.anchor,
                "sha256": self.document_sha256, "section_sha256": self.section_sha256}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def query_terms(query: str) -> list[str]:
    return sorted({w for w in _WORD.findall(query.lower()) if len(w) >= 2})


def query_lexemes(session: Session, query: str) -> list[str]:
    """The question's lexemes as the index sees them (english stemming, stop words dropped)."""
    return sorted(session.scalar(text("SELECT tsvector_to_array(to_tsvector('english', :q))"), {"q": query[:4000]}) or [])


def _tsquery_literal(lexemes: list[str]) -> str:
    """An OR tsquery of already-normalised lexemes (cast, not re-parsed by a dictionary)."""
    return " | ".join("'" + lx.replace("\\", "\\\\").replace("'", "''") + "'" for lx in lexemes)


def _corpus(session: Session, packs: list[str]) -> tuple[int, float]:
    n = tokens = 0
    for p in packs:
        stats = (_state(session, f"pack:{p}") or {}).get("bm25") or _bm25_stats(session, p)  # older state: compute
        n += int(stats.get("sections") or 0)
        tokens += int(stats.get("tokens") or 0)
    return n, (tokens / n if n else 1.0)


def _bm25(session: Session, packs: list[str], query: str, candidates: int) -> tuple[list[str], dict[str, float]]:
    """Okapi BM25 (k1=1.2, b=0.75, idf = ln(1 + (N - df + .5) / (df + .5))) over the tsvector
    lexemes. Every section containing a query lexeme matches the OR query, so document frequencies
    are counted over that match set; N and the mean length come from the per-pack index state.
    Returns the ranked ids and each one's share of the question's lexemes."""
    lexemes = query_lexemes(session, query)
    if not lexemes:
        return [], {}
    n, avgdl = _corpus(session, packs)
    rows = session.execute(text(f"""
        WITH cand AS (SELECT s.id, s.tsv FROM knowledge_section s
                      WHERE s.pack_id = ANY(:packs) AND s.tsv @@ CAST(:tsq AS tsquery)),
             lens AS (SELECT c.id, {_tokens('c.tsv')} AS dl FROM cand c),
             tf AS (SELECT c.id, u.lexeme, coalesce(array_length(u.positions, 1), 1) AS tf
                    FROM cand c, unnest(c.tsv) u WHERE u.lexeme = ANY(:lexemes)),
             df AS (SELECT lexeme, count(*) AS df FROM tf GROUP BY lexeme)
        SELECT tf.id, round(sum(ln(1 + (:n - df.df + 0.5) / (df.df + 0.5)) * tf.tf * (:k1 + 1)
                                / (tf.tf + :k1 * (1 - :b + :b * lens.dl / :avgdl)))::numeric, 8) AS score,
               count(*) AS matched
        FROM tf JOIN df ON df.lexeme = tf.lexeme JOIN lens ON lens.id = tf.id
        GROUP BY tf.id ORDER BY score DESC, tf.id LIMIT :limit"""),
                           {"packs": packs, "tsq": _tsquery_literal(lexemes), "lexemes": lexemes, "n": max(n, 1),
                            "avgdl": max(avgdl, 1.0), "k1": BM25_K1, "b": BM25_B, "limit": candidates}).all()
    return [r.id for r in rows], {r.id: round(int(r.matched) / len(lexemes), 4) for r in rows}


def _lexical(session: Session, packs: list[str], words: list[str], candidates: int) -> list[str]:
    """The P4-K01 lexical leg: `ts_rank_cd` over an OR query of the question's words."""
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
             exclude_kinds: Iterable[str] = (), lexical: str = "bm25", hop: bool = False,
             hop_from: int = 5) -> list[Hit]:
    """Hybrid retrieval over the packs this workspace may see (platform + its own). `pack_ids` can
    only narrow that set. The API the context compiler calls (P4-K05).

    `hop=True` follows the resolved internal links of the best `hop_from` hits one step: a linked
    document's first section joins the results at HOP_DECAY of the linking hit's score (unless it
    is already there), marked with `via`. Links never leave the linking document's pack."""
    from analystos.knowledge.store import visible_packs

    if lexical not in LEXICAL:
        raise ValueError(f"lexical leg must be one of {LEXICAL}")
    visible = {p.id: p for p in visible_packs(session, workspace_id)}
    wanted = [p for p in visible if pack_ids is None or p in set(pack_ids)]
    if not wanted or not query.strip():
        return []
    if lexical == "bm25":
        lex, shares = _bm25(session, wanted, query, candidates)
    else:
        lex, shares = _lexical(session, wanted, query_terms(query), candidates), {}
    vec, sims = _vector(session, wanted, query, candidates)
    fused: dict[str, float] = {}
    for ranks in (lex, vec):
        for n, sid in enumerate(ranks, start=1):
            fused[sid] = fused.get(sid, 0.0) + 1.0 / (RRF_K + n)
    if not fused:
        return []
    kinds_set = set(kinds) if kinds is not None else None
    types_set = {t.lower() for t in types} if types is not None else None
    excluded = set(exclude_kinds)

    def keep(d: KnowledgeDocument) -> bool:
        if (kinds_set is not None and d.kind not in kinds_set) or d.kind in excluded:
            return False
        return types_set is None or d.type.lower() in types_set

    lex_rank = {sid: n for n, sid in enumerate(lex, start=1)}
    vec_rank = {sid: n for n, sid in enumerate(vec, start=1)}
    hits = [_hit(visible[d.pack_id], s, d, round(fused[s.id], 8), lex_rank.get(s.id), vec_rank.get(s.id), sims.get(s.id),
                 shares.get(s.id))
            for s, d in _rows(session, list(fused)) if keep(d)]
    hits.sort(key=lambda h: (-h.score, h.path, h.anchor))
    if hop:
        hits = _one_hop(session, visible, hits, hop_from, keep)
    return hits[:limit]


def _rows(session: Session, section_ids: list[str]) -> list[Any]:
    return list(session.execute(
        select(KnowledgeSection, KnowledgeDocument).join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeSection.document_id)
        .where(KnowledgeSection.id.in_(section_ids))).all())


def _hit(p: KnowledgePack, s: KnowledgeSection, d: KnowledgeDocument, score: float, lexical_rank: int | None,
         vector_rank: int | None, similarity: float | None, share: float | None, via: str | None = None) -> Hit:
    return Hit(section_id=s.id, document_id=d.id, pack_id=p.id, pack_slug=p.slug, pack_kind=p.kind, path=d.path,
               concept_id=d.concept_id, type=d.type, kind=d.kind, title=d.title, anchor=s.anchor, heading=s.heading,
               text=s.text, document_sha256=d.sha256, section_sha256=s.sha256, status=d.status, trust_tier=d.trust_tier,
               score=score, lexical_rank=lexical_rank, vector_rank=vector_rank, similarity=similarity, lexical_share=share,
               via=via)


def _one_hop(session: Session, visible: dict[str, KnowledgePack], hits: list[Hit], hop_from: int, keep: Any) -> list[Hit]:
    """One hop over the link graph: the first section of every document a top hit links to (resolved
    internal links) gains HOP_DECAY of the linking hit's score — added to its own score when it is
    already a hit, as a new hit otherwise — and records `via`. The best linking hit counts once, and
    a link never lifts a section above the hit that links to it (a hop adds context, it does not
    replace the answer)."""
    top = hits[:hop_from]
    if not top:
        return hits
    boost: dict[str, tuple[float, str, float]] = {}  # document id -> (added score, via path, linking score)
    for h in top:
        for target in session.scalars(select(KnowledgeLink.target_path).where(
                KnowledgeLink.pack_id == h.pack_id, KnowledgeLink.source_path == h.path, KnowledgeLink.kind == "internal",
                KnowledgeLink.resolved.is_(True)).order_by(KnowledgeLink.target_path)):
            did = doc_id(h.pack_id, str(target))
            add = round(h.score * HOP_DECAY, 8)
            if did != h.document_id and add > boost.get(did, (0.0, "", 0.0))[0]:
                boost[did] = (add, h.path, h.score)
    if not boost:
        return hits
    firsts = {d.id: (s, d) for s, d in session.execute(
        select(KnowledgeSection, KnowledgeDocument).join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeSection.document_id)
        .where(KnowledgeSection.document_id.in_(list(boost)), KnowledgeSection.ordinal == 0)).all()
        if d.pack_id in visible and keep(d)}
    out, seen = [], set()
    for h in hits:
        if h.document_id in firsts and h.section_id == firsts[h.document_id][0].id:
            add, via, ceiling = boost[h.document_id]
            if h.score < ceiling:
                h = dataclasses.replace(h, score=round(min(h.score + add, ceiling - 1e-8), 8), via=via)
            seen.add(h.document_id)
        out.append(h)
    for did, (s, d) in firsts.items():
        if did not in seen:
            out.append(_hit(visible[d.pack_id], s, d, boost[did][0], None, None, None, None, via=boost[did][1]))
    out.sort(key=lambda h: (-h.score, h.path, h.anchor))
    return out


def signature(hits: Iterable[Hit]) -> list[tuple[Any, ...]]:
    """What "identical retrieval results" compares: ids, order, scores and ranks."""
    return [(h.section_id, h.path, h.anchor, h.score, h.lexical_rank, h.vector_rank, h.similarity) for h in hits]


def stats(session: Session) -> dict[str, Any]:
    counts = {t: session.scalar(text(f"SELECT count(*) FROM {t}"))
              for t in ("knowledge_document", "knowledge_section", "knowledge_link")}
    hnsw = session.scalar(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'knowledge_section' "
                               "AND indexname = :n"), {"n": HNSW_INDEX})
    return {**counts, "hnsw_index": hnsw, "embedding": index_embedding(session)}
