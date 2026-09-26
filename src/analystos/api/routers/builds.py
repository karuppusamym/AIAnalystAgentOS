"""Build API (P4-E04/E06): designated target schemas, `elt_build` runs and their build jobs.

Approving a build uses the approvals inbox like every other side effect (`/api/approvals/{id}/decide`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.build import service as build_svc
from analystos.build.targets import DEFAULT_ENGINE
from analystos.db.models import Approval, BuildJob, BuildTarget, User
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader

router = APIRouter(prefix="/api", tags=["builds"])
_SUMMARY_EXCLUDE = {"project_files", "openlineage", "log_tail", "manifest"}


class BuildTargetIn(BaseModel):
    schema_name: str
    engine: str = DEFAULT_ENGINE


class BuildIn(BaseModel):
    from_run_id: str
    target_schema: str
    engine: str = DEFAULT_ENGINE
    autonomy_level: int | None = None


@router.get("/workspaces/{workspace_id}/build-targets")
def list_build_targets(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return rows(session.scalars(select(BuildTarget).where(BuildTarget.workspace_id == workspace_id)
                                .order_by(BuildTarget.created_at)))


@router.post("/workspaces/{workspace_id}/build-targets")
def designate_build_target(workspace_id: str, body: BuildTargetIn, user: User = Depends(current_user),
                           session: Session = Depends(db, scope="function")):
    return row(build_svc.designate_target(session, session.merge(user), workspace_id, body.schema_name, engine=body.engine))


@router.post("/workspaces/{workspace_id}/builds")
def start_build(workspace_id: str, body: BuildIn, user: User = Depends(current_user)):
    run = build_svc.start_build(user, workspace_id, from_run_id=body.from_run_id, target_schema=body.target_schema,
                                engine=body.engine, autonomy_level=body.autonomy_level)
    return {"run_id": run.id, "status": run.status, "playbook": build_svc.PLAYBOOK}


@router.get("/workspaces/{workspace_id}/builds")
def list_builds(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    jobs = session.scalars(select(BuildJob).where(BuildJob.workspace_id == workspace_id).order_by(BuildJob.created_at.desc()))
    out = rows(jobs, exclude=_SUMMARY_EXCLUDE)
    ids = [j["approval_id"] for j in out if j.get("approval_id")]
    status = dict(session.execute(select(Approval.id, Approval.status).where(Approval.id.in_(ids))).all()) if ids else {}
    for j in out:
        j["approval_status"] = status.get(j.get("approval_id"))
    return out


@scoped_loader
def _job(session: Session, user: User, job_id: str) -> BuildJob:
    return load_in_workspace(session, BuildJob, job_id, user=user, label="build job")


@router.get("/builds/{job_id}")
def get_build(job_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    job = _job(session, user, job_id)
    return {**row(job), "approval": build_svc.approval_summary(session, job)}


@router.get("/builds/{job_id}/diff")
def build_diff(job_id: str, against: str | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The generated dbt project file by file against the previous job for the same target (or `against`,
    another job of the same workspace). Read-only: what the approver reviews, not what they approve —
    the approval binds the whole project hash."""
    return build_svc.job_diff(session, _job(session, user, job_id), against)
