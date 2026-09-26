"""Artifact registry + lineage (ART-001..003). Every query, profile, dataset, metric, chart,
dashboard and report is persisted and versioned; provenance is a generic edge list that the
graph projection mirrors into Neo4j."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from sqlalchemy import Integer, String, and_, case, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analystos.core.ids import new_id, stable_hash
from analystos.db.models import AnalysisRun, Artifact, ArtifactVersion, LineageEdge

ARTIFACT_TYPES = {"query", "profile", "quality_report", "relationship_map", "context_package", "plan", "dataset",
                  "semantic_model", "metric", "chart", "dashboard", "report", "narrative", "python_script", "ml_model",
                  "forecast", "alert", "schedule", "export", "data_quality_rule", "notebook", "transformation",
                  "agent_output"}

# (run_id, plan_version) of the task currently executing; set by the engine around each task.
producing_plan: ContextVar[tuple[str, int] | None] = ContextVar("producing_plan", default=None)


def _plan_version(session: Session, run_id: str | None) -> int | None:
    if run_id is None:
        return None
    producing = producing_plan.get()
    if producing and producing[0] == run_id:
        return producing[1]
    run = session.get(AnalysisRun, run_id)
    return run.plan_version if run is not None else None


def current_plan_filter(run: AnalysisRun):
    """Artifacts written under the run's current plan version (unstamped rows predate stamping)."""
    return or_(Artifact.plan_version == run.plan_version, Artifact.plan_version.is_(None))


def save_artifact(session: Session, *, workspace_id: str, type_: str, name: str, content: dict[str, Any],
                  run_id: str | None = None, creator_agent: str | None = None, creator_user: str | None = None,
                  status: str = "draft") -> Artifact:
    """Upsert by (workspace, run, type, name): unchanged content is a no-op, changed content is a new version.

    An agent writing inside a run passes the ``artifact.write`` tool gate (workspace ``tool_denylist``,
    role, autonomy; spec v2 §6) and its manifest's ``output_contract`` (the type must be declared and the
    content must match the declared schema, else OutputContractViolation and nothing is written).
    Writes by a signed-in user are governed by their route's role check.
    Run artifacts are stamped with the writing task's plan version; a task of a superseded plan cannot
    overwrite what the current plan already wrote."""
    if type_ not in ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact type {type_}")
    if creator_agent and run_id:
        from analystos.capabilities.agents import contract_for, enforce_output
        from analystos.tools.registry import gate_agent_write

        gate_agent_write(session, workspace_id=workspace_id, run_id=run_id, agent_id=creator_agent,
                         inputs={"type": type_, "name": name})
        # The agent's output contract (FND-006), as the run bound it: declared type, declared schema.
        enforce_output(contract_for(session.get(AnalysisRun, run_id), creator_agent), creator_agent, type_, content)
    content_hash = stable_hash(content)
    plan_version = _plan_version(session, run_id)
    existing = session.scalar(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.run_id == run_id,
                                                     Artifact.type == type_, Artifact.name == name))
    if existing:
        if plan_version is not None and (existing.plan_version or 0) > plan_version:
            return existing
        if plan_version is not None:
            existing.plan_version = plan_version
        if existing.content_hash != content_hash:
            existing.version += 1
            existing.content = content
            existing.content_hash = content_hash
            existing.status = status
            session.add(ArtifactVersion(artifact_id=existing.id, version=existing.version, content=content,
                                        content_hash=content_hash, created_by=creator_agent or creator_user))
        return existing
    artifact = Artifact(id=new_id("art"), workspace_id=workspace_id, run_id=run_id, type=type_, name=name, version=1,
                        plan_version=plan_version, status=status, creator_agent=creator_agent, creator_user=creator_user,
                        content=content, content_hash=content_hash)
    session.add(artifact)
    session.flush()
    session.add(ArtifactVersion(artifact_id=artifact.id, version=1, content=content, content_hash=content_hash,
                                created_by=creator_agent or creator_user))
    return artifact


def link(session: Session, workspace_id: str, from_: tuple[str, str], relation: str, to: tuple[str, str],
         run_id: str | None = None) -> None:
    stmt = insert(LineageEdge).values(workspace_id=workspace_id, run_id=run_id, from_type=from_[0], from_id=str(from_[1]),
                                      relation=relation, to_type=to[0], to_id=str(to[1]))
    session.execute(stmt.on_conflict_do_nothing(index_elements=["workspace_id", "from_type", "from_id", "relation",
                                                                 "to_type", "to_id"]))


def lineage_for(session: Session, workspace_id: str, node: tuple[str, str], *, depth: int = 6) -> dict[str, Any]:
    """Upstream + downstream provenance around a node: every edge touching a node within `depth - 1`
    hops (either direction), and those edges' ends. One recursive CTE over this workspace's edges
    (P4-S02); it reads the neighbourhood only, never the whole workspace."""
    node = (node[0], str(node[1]))
    if depth <= 0:
        return {"nodes": [{"type": node[0], "id": node[1]}], "edges": []}
    e = LineageEdge

    def touches(t, i):  # noqa: ANN001, ANN202 - either end of a workspace edge (both ends are indexed)
        return and_(e.workspace_id == workspace_id,
                    or_(and_(e.from_type == t, e.from_id == i), and_(e.to_type == t, e.to_id == i)))

    reach = select(literal(node[0], String).label("t"), literal(node[1], String).label("i"),
                   literal(0, Integer).label("d")).cte("reach", recursive=True)
    from_end = and_(e.from_type == reach.c.t, e.from_id == reach.c.i)
    reach = reach.union(select(case((from_end, e.to_type), else_=e.from_type), case((from_end, e.to_id), else_=e.from_id),
                               reach.c.d + 1)
                        .join_from(reach, e, touches(reach.c.t, reach.c.i)).where(reach.c.d < depth - 1))
    near = select(reach.c.t, reach.c.i).distinct().subquery("near")
    touching = select(e.id).join_from(near, e, touches(near.c.t, near.c.i))
    rows = session.execute(select(e.id, e.from_type, e.from_id, e.relation, e.to_type, e.to_id)
                           .where(e.id.in_(touching)).order_by(e.id)).all()
    nodes = {node} | {(r.from_type, r.from_id) for r in rows} | {(r.to_type, r.to_id) for r in rows}
    return {"nodes": [{"type": t, "id": i} for t, i in sorted(nodes)],
            "edges": [{"from": [r.from_type, r.from_id], "relation": r.relation, "to": [r.to_type, r.to_id]}
                      for r in rows]}
