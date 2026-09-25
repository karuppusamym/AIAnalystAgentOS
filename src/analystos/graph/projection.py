"""Analytics knowledge graph (§30). Neo4j is a projection of the Postgres lineage/relationship
tables, rebuilt idempotently (MERGE), so a Neo4j outage never loses provenance."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.config import get_settings
from analystos.core.logging import get_logger
from analystos.db.models import LineageEdge, Relationship, SourceAsset

log = get_logger(__name__)
LABELS = {"objective": "BusinessObjective", "run": "AnalysisRun", "hypothesis": "Hypothesis", "insight": "Finding",
          "experiment": "Evidence", "query": "Query", "table": "Table", "column": "Column", "dataset": "Dataset",
          "metric": "Metric", "chart": "Chart", "dashboard": "Dashboard", "artifact": "Artifact", "source": "SourceSystem",
          "agent": "Agent", "tool": "Tool", "user": "User", "approval": "Decision", "publication": "Publication",
          "profile": "Artifact", "quality_report": "Artifact", "context_package": "Artifact", "plan": "Artifact",
          "semantic_model": "SemanticModel", "feedback": "Feedback"}


@lru_cache
def _driver():
    from neo4j import GraphDatabase

    s = get_settings()
    return GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password), connection_timeout=3)


def ensure_schema(g) -> None:  # noqa: ANN001
    """Nodes are keyed by (workspace_id, type, id): the same table name in two workspaces is two nodes.
    The earlier (type, id) constraint let the last projection's workspace overwrite the node."""
    g.run("DROP CONSTRAINT aos_node IF EXISTS")
    g.run("CREATE CONSTRAINT aos_node_ws IF NOT EXISTS FOR (n:AOS) REQUIRE (n.workspace_id, n.type, n.id) IS UNIQUE")


def project_workspace(session: Session, workspace_id: str) -> dict:
    """MERGE all lineage edges and table relationships of a workspace into Neo4j."""
    edges = list(session.scalars(select(LineageEdge).where(LineageEdge.workspace_id == workspace_id)))
    rels = list(session.scalars(select(Relationship).where(Relationship.workspace_id == workspace_id)))
    assets = {a.id: f"{a.schema_name}.{a.name}" for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id))}
    rows = [{"ft": e.from_type, "fid": e.from_id, "fl": LABELS.get(e.from_type, "Artifact"), "rel": e.relation.upper(),
             "tt": e.to_type, "tid": e.to_id, "tl": LABELS.get(e.to_type, "Artifact")} for e in edges]
    rel_rows = [{"a": assets.get(r.from_asset_id, r.from_asset_id), "ac": r.from_column, "b": assets.get(r.to_asset_id, r.to_asset_id),
                 "bc": r.to_column, "card": r.cardinality, "conf": r.confidence} for r in rels]
    try:
        with _driver().session() as g:
            ensure_schema(g)
            by_rel: dict[tuple, list] = {}
            for r in rows:
                by_rel.setdefault((r["fl"], r["rel"], r["tl"]), []).append(r)
            for (fl, rel, tl), batch in by_rel.items():
                g.run(f"UNWIND $rows AS r MERGE (a:AOS {{workspace_id: $ws, type: r.ft, id: r.fid}}) SET a:{fl} "
                      f"MERGE (b:AOS {{workspace_id: $ws, type: r.tt, id: r.tid}}) SET b:{tl} "
                      f"MERGE (a)-[:{rel}]->(b)", rows=batch, ws=workspace_id)
            g.run("UNWIND $rows AS r MERGE (a:AOS {workspace_id: $ws, type:'table', id:r.a}) SET a:Table "
                  "MERGE (b:AOS {workspace_id: $ws, type:'table', id:r.b}) SET b:Table "
                  "MERGE (a)-[j:JOINS_TO {from_column:r.ac, to_column:r.bc}]->(b) SET j.cardinality=r.card, j.confidence=r.conf",
                  rows=rel_rows, ws=workspace_id)
        return {"ok": True, "edges": len(rows), "relationships": len(rel_rows)}
    except Exception as exc:  # projection is best effort; Postgres stays authoritative
        log.warning("neo4j projection failed: %s", exc)
        return {"ok": False, "error": str(exc)[:300]}


def neighborhood(tables: list[str], workspace_id: str) -> list[dict]:
    """Graph neighborhood for context retrieval: joins and prior findings touching these tables."""
    try:
        with _driver().session() as g:
            # Both ends are pinned to the workspace, so a stray cross-workspace edge is never followed.
            result = g.run("MATCH (t:AOS {workspace_id: $ws, type:'table'})-[r]-(n:AOS {workspace_id: $ws}) "
                           "WHERE t.id IN $tables "
                           "RETURN t.id AS table, type(r) AS rel, n.type AS type, n.id AS id LIMIT 200",
                           tables=tables, ws=workspace_id)
            return [dict(r) for r in result]
    except Exception as exc:
        log.warning("neo4j neighborhood failed: %s", exc)
        return []
