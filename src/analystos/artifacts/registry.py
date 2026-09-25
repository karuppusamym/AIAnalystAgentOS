"""Artifact registry + lineage (ART-001..003). Every query, profile, dataset, metric, chart,
dashboard and report is persisted and versioned; provenance is a generic edge list that the
graph projection mirrors into Neo4j."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analystos.core.ids import new_id, stable_hash
from analystos.db.models import Artifact, ArtifactVersion, LineageEdge

ARTIFACT_TYPES = {"query", "profile", "quality_report", "relationship_map", "context_package", "plan", "dataset",
                  "semantic_model", "metric", "chart", "dashboard", "report", "narrative", "python_script", "ml_model",
                  "forecast", "alert", "schedule", "export", "data_quality_rule", "notebook", "transformation"}


def save_artifact(session: Session, *, workspace_id: str, type_: str, name: str, content: dict[str, Any],
                  run_id: str | None = None, creator_agent: str | None = None, creator_user: str | None = None,
                  status: str = "draft") -> Artifact:
    """Upsert by (workspace, run, type, name): unchanged content is a no-op, changed content is a new version."""
    if type_ not in ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact type {type_}")
    content_hash = stable_hash(content)
    existing = session.scalar(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.run_id == run_id,
                                                     Artifact.type == type_, Artifact.name == name))
    if existing:
        if existing.content_hash != content_hash:
            existing.version += 1
            existing.content = content
            existing.content_hash = content_hash
            existing.status = status
            session.add(ArtifactVersion(artifact_id=existing.id, version=existing.version, content=content,
                                        content_hash=content_hash, created_by=creator_agent or creator_user))
        return existing
    artifact = Artifact(id=new_id("art"), workspace_id=workspace_id, run_id=run_id, type=type_, name=name, version=1,
                        status=status, creator_agent=creator_agent, creator_user=creator_user, content=content,
                        content_hash=content_hash)
    session.add(artifact)
    session.flush()
    session.add(ArtifactVersion(artifact_id=artifact.id, version=1, content=content, content_hash=content_hash,
                                created_by=creator_agent or creator_user))
    return artifact


def link(session: Session, workspace_id: str, from_: tuple[str, str], relation: str, to: tuple[str, str],
         run_id: str | None = None) -> None:
    stmt = insert(LineageEdge).values(workspace_id=workspace_id, run_id=run_id, from_type=from_[0], from_id=str(from_[1]),
                                      relation=relation, to_type=to[0], to_id=str(to[1]))
    session.execute(stmt.on_conflict_do_nothing(index_elements=["from_type", "from_id", "relation", "to_type", "to_id"]))


def lineage_for(session: Session, workspace_id: str, node: tuple[str, str], *, depth: int = 6) -> dict[str, Any]:
    """Upstream + downstream provenance around a node (BFS over edges, both directions)."""
    edges = list(session.scalars(select(LineageEdge).where(LineageEdge.workspace_id == workspace_id)))
    out_adj: dict[tuple[str, str], list[LineageEdge]] = {}
    in_adj: dict[tuple[str, str], list[LineageEdge]] = {}
    for e in edges:
        out_adj.setdefault((e.from_type, e.from_id), []).append(e)
        in_adj.setdefault((e.to_type, e.to_id), []).append(e)
    seen_nodes = {node}
    seen_edges: set[int] = set()
    frontier = [node]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for e in out_adj.get(n, []) + in_adj.get(n, []):
                if e.id in seen_edges:
                    continue
                seen_edges.add(e.id)
                for m in ((e.from_type, e.from_id), (e.to_type, e.to_id)):
                    if m not in seen_nodes:
                        seen_nodes.add(m)
                        nxt.append(m)
        frontier = nxt
    by_id = {e.id: e for e in edges}
    return {"nodes": [{"type": t, "id": i} for t, i in sorted(seen_nodes)],
            "edges": [{"from": [by_id[i].from_type, by_id[i].from_id], "relation": by_id[i].relation,
                       "to": [by_id[i].to_type, by_id[i].to_id]} for i in sorted(seen_edges)]}
