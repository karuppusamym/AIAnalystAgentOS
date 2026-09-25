"""Context service (§11). Layered retrieval:
  1 exact metadata  2 graph neighborhood  3 vector search  4 prior artifacts/episodes
  5 user-provided context (feedback)  6 source inspection happens in the metadata/profiler agents.

Knowledge comes from the workspace's `context_entry` rows and from the knowledge packs (platform
pack, workspace pack, imported bundles) through the knowledge index (P4-K01). Other knowledge
sources are context providers (knowledge/providers.py, P4-K09: `okf_import`, Atlas over `mcp`);
the speculative REST adapter to a Context2AI service was retired with them.
Context is DATA: agents quote it inside an untrusted-context envelope and it can never grant tools
or widen scope."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.context.embeddings import embed
from analystos.core.ids import new_id
from analystos.core.logging import get_logger
from analystos.db.models import ContextEntry, KnowledgeDocument, SourceAsset, SourceColumn
from analystos.graph.projection import neighborhood

log = get_logger(__name__)


def add_entry(session: Session, *, workspace_id: str, kind: str, name: str, body: str,
              synonyms: list[str] | None = None, mapped_columns: list[str] | None = None, origin: str = "user",
              trusted: bool = True) -> ContextEntry:
    if not workspace_id:
        raise ValueError("context entries belong to a workspace; platform knowledge lives in the platform pack")
    entry = ContextEntry(id=new_id("ctx"), workspace_id=workspace_id, kind=kind, name=name, body=body,
                         synonyms=synonyms or [], mapped_columns=mapped_columns or [], origin=origin, trusted=trusted,
                         embedding=embed(" ".join([name, body, *(synonyms or [])])))
    session.add(entry)
    return entry


def search(session: Session, workspace_id: str, query: str, *, limit: int = 8, kinds: list[str] | None = None) -> list[dict]:
    """The workspace's own entries (cosine over their hashing embeddings) merged with pack knowledge
    (hybrid index retrieval, scored by its vector similarity), best first."""
    from analystos.knowledge.index import retrieve

    vec = embed(query)
    stmt = select(ContextEntry, ContextEntry.embedding.cosine_distance(vec).label("dist")).where(
        ContextEntry.workspace_id == workspace_id)
    if kinds:
        stmt = stmt.where(ContextEntry.kind.in_(kinds))
    rows = session.execute(stmt.order_by("dist").limit(limit)).all()
    out = [{"id": e.id, "kind": e.kind, "name": e.name, "body": e.body, "synonyms": e.synonyms,
            "mapped_columns": e.mapped_columns, "origin": e.origin, "score": round(1 - float(d), 4) if d is not None else 0.0}
           for e, d in rows]
    seen: set[str] = set()
    for h in retrieve(session, workspace_id, query, limit=limit * 3, kinds=kinds):
        if h.document_id in seen:
            continue  # one entry per document: its best section
        seen.add(h.document_id)
        out.append(_hit_entry(session, h))
        if len(seen) >= limit:
            break
    out.sort(key=lambda r: (-r["score"], r["name"], r["id"]))
    return out[:limit]


def _hit_entry(session: Session, h: Any) -> dict[str, Any]:
    doc = session.get(KnowledgeDocument, h.document_id)
    ext = (doc.frontmatter or {}).get("analystos") if doc is not None else None
    ext = ext if isinstance(ext, dict) else {}
    return {"id": h.document_id, "kind": h.kind, "name": h.title, "body": h.text, "synonyms": list(ext.get("synonyms") or []),
            "mapped_columns": list(ext.get("mapped_columns") or []),
            "origin": str(ext.get("origin") or f"okf:{h.pack_slug}"), "score": round(float(h.similarity or 0.0), 4),
            "receipt": h.receipt()}


def external_context(session: Session, workspace_id: str, objective: str, *, user: Any = None,
                     run_id: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """Results of the workspace's non-local context providers (imported bundles, Atlas over MCP).
    A provider that fails reports UNAVAILABLE; it never fails the run."""
    from analystos.governance.policy import get_workspace, load_policy
    from analystos.knowledge.providers import for_workspace

    out = []
    for provider in for_workspace(load_policy(session, get_workspace(session, workspace_id))):
        if provider.kind == "local":
            continue
        try:
            out.append(provider.retrieve(session, workspace_id, objective, limit=limit, user=user, run_id=run_id).as_dict())
        except Exception as exc:  # noqa: BLE001 - context is best effort
            log.warning("context provider %s failed: %s", provider.kind, type(exc).__name__)
            out.append({"provider": provider.kind, "status": "UNAVAILABLE", "detail": {"reason": type(exc).__name__},
                        "items": [], "receipts": []})
    return out


def build_context_package(session: Session, workspace_id: str, objective: str, assets: list[str],
                          extra_notes: list[str] | None = None, *, user: Any = None,
                          run_id: str | None = None) -> dict[str, Any]:
    # 1 exact metadata
    tables = []
    for fq in assets:
        schema, name = fq.split(".", 1)
        asset = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                         SourceAsset.schema_name == schema, SourceAsset.name == name))
        if not asset:
            continue
        cols = list(session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal)))
        tables.append({"table": fq, "business_name": asset.business_name, "description": asset.description,
                       "row_count": asset.row_count,
                       "columns": [{"name": c.name, "type": c.data_type, "semantic_type": c.semantic_type,
                                    "business_name": c.business_name, "description": c.description, "tags": c.tags}
                                   for c in cols]})
    # 2 graph neighborhood
    graph = neighborhood(assets, workspace_id, session)
    # 3 vector search (+ the workspace's other context providers)
    terms = search(session, workspace_id, objective, limit=12)
    external = external_context(session, workspace_id, objective, user=user, run_id=run_id)
    # 4 prior episodes / known dashboards and metrics
    episodes = search(session, workspace_id, objective, limit=3, kinds=["episode"])
    metrics = search(session, workspace_id, objective, limit=8, kinds=["metric"])
    # Ambiguity: objective words that map to no term, table or column
    known = {t["name"].lower() for t in terms if t["score"] > 0.35} | {c["name"].lower() for t in tables for c in t["columns"]}
    return {"objective": objective, "tables": tables, "glossary": [t for t in terms if t["kind"] != "episode"],
            "metrics": metrics, "graph": graph, "episodes": episodes, "external": external,
            "user_notes": extra_notes or [], "known_terms": sorted(known)[:200],
            "retrieval_layers": ["exact_metadata", "graph_neighborhood", "vector_search", "prior_artifacts", "user_context"]}
