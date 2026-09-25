"""Context service adapter (§11). Layered retrieval:
  1 exact metadata  2 graph neighborhood  3 vector search  4 prior artifacts/episodes
  5 user-provided context (feedback)  6 source inspection happens in the metadata/profiler agents.
Context2AI is used when configured; otherwise the local store (context_entry) serves the same API.
Context is DATA: agents quote it inside an untrusted-context envelope and it can never grant tools
or widen scope."""
from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.context.embeddings import embed
from analystos.core.config import get_settings
from analystos.core.ids import new_id
from analystos.core.logging import get_logger
from analystos.db.models import ContextEntry, SourceAsset, SourceColumn
from analystos.graph.projection import neighborhood

log = get_logger(__name__)


class Context2AIClient:
    """HTTP client for an existing Context2AI deployment (CTX-001). Endpoints per spec §11.1."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=10,
                                   headers={"Authorization": f"Bearer {token}"} if token else {})

    def search(self, query: str, workspace_id: str, limit: int = 10) -> list[dict[str, Any]]:
        r = self.client.post("/context/search", json={"query": query, "workspace_id": workspace_id, "limit": limit})
        r.raise_for_status()
        return r.json().get("results", [])

    def metrics(self) -> list[dict[str, Any]]:
        r = self.client.get("/context/metrics")
        r.raise_for_status()
        return r.json()


def add_entry(session: Session, *, workspace_id: str | None, kind: str, name: str, body: str,
              synonyms: list[str] | None = None, mapped_columns: list[str] | None = None, origin: str = "user",
              trusted: bool = True) -> ContextEntry:
    entry = ContextEntry(id=new_id("ctx"), workspace_id=workspace_id, kind=kind, name=name, body=body,
                         synonyms=synonyms or [], mapped_columns=mapped_columns or [], origin=origin, trusted=trusted,
                         embedding=embed(" ".join([name, body, *(synonyms or [])])))
    session.add(entry)
    return entry


def search(session: Session, workspace_id: str, query: str, *, limit: int = 8, kinds: list[str] | None = None) -> list[dict]:
    vec = embed(query)
    stmt = select(ContextEntry, ContextEntry.embedding.cosine_distance(vec).label("dist")).where(
        or_(ContextEntry.workspace_id == workspace_id, ContextEntry.workspace_id.is_(None)))
    if kinds:
        stmt = stmt.where(ContextEntry.kind.in_(kinds))
    rows = session.execute(stmt.order_by("dist").limit(limit)).all()
    return [{"id": e.id, "kind": e.kind, "name": e.name, "body": e.body, "synonyms": e.synonyms,
             "mapped_columns": e.mapped_columns, "origin": e.origin, "score": round(1 - float(d), 4)} for e, d in rows]


def build_context_package(session: Session, workspace_id: str, objective: str, assets: list[str],
                          extra_notes: list[str] | None = None) -> dict[str, Any]:
    settings = get_settings()
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
    # 3 vector search (+ Context2AI when configured)
    terms = search(session, workspace_id, objective, limit=12)
    external: list[dict] = []
    if settings.context2ai_url:
        try:
            external = Context2AIClient(settings.context2ai_url).search(objective, workspace_id)
        except Exception as exc:
            log.warning("Context2AI unavailable, using local context only: %s", exc)
    # 4 prior episodes / known dashboards and metrics
    episodes = search(session, workspace_id, objective, limit=3, kinds=["episode"])
    metrics = search(session, workspace_id, objective, limit=8, kinds=["metric"])
    # Ambiguity: objective words that map to no term, table or column
    known = {t["name"].lower() for t in terms if t["score"] > 0.35} | {c["name"].lower() for t in tables for c in t["columns"]}
    return {"objective": objective, "tables": tables, "glossary": [t for t in terms if t["kind"] != "episode"],
            "metrics": metrics, "graph": graph, "episodes": episodes, "external": external,
            "user_notes": extra_notes or [], "known_terms": sorted(known)[:200],
            "retrieval_layers": ["exact_metadata", "graph_neighborhood", "vector_search", "prior_artifacts", "user_context"]}
