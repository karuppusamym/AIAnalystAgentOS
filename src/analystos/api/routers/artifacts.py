from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.artifacts.registry import lineage_for
from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import (
    AnalysisRun,
    Approval,
    Artifact,
    ArtifactVersion,
    Insight,
    Publication,
    QueryExecution,
    User,
)
from analystos.governance import approvals as approval_svc
from analystos.governance.policy import require_role
from analystos.semantic import service as semantic_svc
from analystos.workflows.orchestrator import signal_run

router = APIRouter(prefix="/api", tags=["artifacts"])


class Decision(BaseModel):
    reason: str | None = None


@router.get("/workspaces/{workspace_id}/artifacts")
def list_artifacts(workspace_id: str, type: str | None = None, run_id: str | None = None, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(Artifact).where(Artifact.workspace_id == workspace_id)
    if type:
        stmt = stmt.where(Artifact.type == type)
    if run_id:
        stmt = stmt.where(Artifact.run_id == run_id)
    return rows(session.scalars(stmt.order_by(Artifact.created_at.desc()).limit(500)))


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    art = session.get(Artifact, artifact_id)
    if art is None:
        raise NotFound("artifact not found")
    require_role(session, user, art.workspace_id, "viewer")
    versions = session.scalars(select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id).order_by(ArtifactVersion.version))
    return {**row(art), "versions": rows(versions, exclude={"content"}),
            "lineage": lineage_for(session, art.workspace_id, (art.type, art.id))}


@router.get("/workspaces/{workspace_id}/lineage")
def lineage(workspace_id: str, node_type: str, node_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return lineage_for(session, workspace_id, (node_type, node_id))


@router.get("/queries/{query_id}")
def get_query(query_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    q = session.get(QueryExecution, query_id)
    if q is None:
        raise NotFound("query not found")
    require_role(session, user, q.workspace_id, "viewer")
    return row(q)


@router.get("/workspaces/{workspace_id}/insights")
def insights(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return rows(session.scalars(select(Insight).where(Insight.workspace_id == workspace_id, Insight.status != "superseded")
                                .order_by(Insight.created_at.desc())))


@router.get("/insights/{insight_id}")
def get_insight(insight_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Evidence behind a finding (DoD #20): experiments, queries (with SQL and result preview), lineage."""
    ins = session.get(Insight, insight_id)
    if ins is None:
        raise NotFound("insight not found")
    require_role(session, user, ins.workspace_id, "viewer")
    q_ids = [e["id"] for e in ins.evidence if e.get("type") == "query"]
    from analystos.db.models import Experiment

    return {**row(ins), "queries": rows(session.scalars(select(QueryExecution).where(QueryExecution.id.in_(q_ids)))),
            "experiments": rows(session.scalars(select(Experiment).where(Experiment.hypothesis_id == ins.hypothesis_id))),
            "lineage": lineage_for(session, ins.workspace_id, ("insight", ins.id), depth=3)}


# ---------------------------------------------------------------------------------- approvals
@router.get("/workspaces/{workspace_id}/approvals")
def list_approvals(workspace_id: str, status: str | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(Approval).where(Approval.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Approval.status == status)
    return rows(session.scalars(stmt.order_by(Approval.created_at.desc())))


def _decide(approval_id: str, user: User, session: Session, approve: bool, reason: str | None):
    approval = approval_svc.decide(session, approval_id, session.merge(user), approve=approve, reason=reason)
    if approval.action == semantic_svc.APPROVAL_ACTION:  # a KPI decided from the approvals inbox takes effect now
        semantic_svc.apply_decision(session, approval)
    session.flush()
    result = row(approval, exclude={"payload"})
    run_id = approval.run_id
    session.commit()
    if run_id:
        signal_run(run_id)
    return result


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, body: Decision | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return _decide(approval_id, user, session, True, body.reason if body else None)


@router.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, body: Decision | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return _decide(approval_id, user, session, False, body.reason if body else None)


def _pending_for_artifact(session: Session, art: Artifact) -> Approval:
    approval = session.scalar(select(Approval).where(Approval.run_id == art.run_id, Approval.action == "publish_dashboard",
                                                     Approval.status == "pending").order_by(Approval.created_at.desc()))
    if approval is None:
        raise InvalidInput("no pending publication proposal for this artifact's run")
    return approval


@router.post("/artifacts/{artifact_id}/publish")
def publish_artifact(artifact_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Publishing is proposal-based: returns the pending, hash-bound proposal that includes this artifact."""
    art = session.get(Artifact, artifact_id)
    if art is None:
        raise NotFound("artifact not found")
    require_role(session, user, art.workspace_id, "editor")
    return row(_pending_for_artifact(session, art), exclude={"payload"})


@router.post("/artifacts/{artifact_id}/approve")
def approve_artifact(artifact_id: str, body: Decision | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    art = session.get(Artifact, artifact_id)
    if art is None:
        raise NotFound("artifact not found")
    return _decide(_pending_for_artifact(session, art).id, user, session, True, body.reason if body else None)


# ---------------------------------------------------------------------------------- dashboards
@router.post("/workspaces/{workspace_id}/dashboards")
def create_dashboards(workspace_id: str, body: dict, user: User = Depends(current_user)):
    """Dashboards are produced by an analysis run (design -> approval -> publish). This starts one."""
    from analystos.services.runs import create_run

    run = create_run(user, workspace_id, objective=body.get("objective"), source_ids=body.get("source_ids"))
    return {"run_id": run.id, "note": "dashboards will be designed by the run and proposed for approval"}


@router.post("/dashboards/{artifact_id}/publish")
def publish_dashboard(artifact_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return publish_artifact(artifact_id, user, session)


@router.post("/dashboards/{artifact_id}/schedule")
def schedule_dashboard(artifact_id: str, user: User = Depends(current_user)):
    raise InvalidInput("create a schedule with POST /api/workspaces/{id}/schedules (kind 'reanalysis' refreshes findings and "
                       "reports; publication of a refreshed dashboard always goes through a new approval)")


@router.post("/publications/{publication_id}/rollback")
def rollback(publication_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    from analystos.core.config import get_settings
    from analystos.governance.audit import audit
    from analystos.publishing.base import get_publisher

    pub = session.get(Publication, publication_id)
    if pub is None:
        raise NotFound("publication not found")
    require_role(session, user, pub.workspace_id, "editor")
    removed = get_publisher(pub.destination, get_settings()).rollback(pub.external_ids)
    pub.status = "rolled_back"
    for art in session.scalars(select(Artifact).where(Artifact.run_id == pub.run_id, Artifact.status == "published")):
        art.status = "rolled_back"
    audit(f"user:{user.id}", "publication.rolled_back", workspace_id=pub.workspace_id, target=pub.id, details={"removed": removed},
          session=session)
    run = session.get(AnalysisRun, pub.run_id) if pub.run_id else None
    return {"removed": removed, "run_id": run.id if run else None}
