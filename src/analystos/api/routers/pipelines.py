"""Pipeline API (P6-01..P6-03): PipelineSpec versions, dry runs, runs, managed-writer destinations,
materialization under a hash-bound approval, rollback, and watermark refresh of staged tables (P6-02).
Every child id is bound to the path's workspace through a scoped loader. Documented in
docs/30-runbooks/10-pipelines-api.md."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.models import User
from analystos.governance.policy import require_role
from analystos.services import pipelines as svc

router = APIRouter(prefix="/api", tags=["pipelines"])


class PipelineIn(BaseModel):
    spec: dict[str, Any]


class DryRunIn(BaseModel):
    engine: Literal["auto", "sql", "duckdb"] | None = None


class RunIn(BaseModel):
    mode: Literal["auto", "full", "reconcile", "replay", "backfill"] = "auto"
    since: datetime | float | None = None  # replay / backfill: the watermark range
    until: datetime | float | None = None
    engine: Literal["auto", "sql", "duckdb"] | None = None


class MaterializeIn(BaseModel):
    approval_id: str | None = None


class DestinationIn(BaseModel):
    schema_name: str = Field(alias="schema")
    tables: list[str] = Field(default_factory=list)
    engine: str = svc.DEFAULT_ENGINE


class RefreshIn(BaseModel):
    asset: str
    mode: Literal["full", "reconcile", "replay", "backfill"]
    since: datetime | float | None = None
    until: datetime | float | None = None


def _engine(value: str | None) -> str | None:
    return None if value in (None, "auto") else value


@router.post("/workspaces/{workspace_id}/pipelines/validate")
def validate(workspace_id: str, body: PipelineIn, user: User = Depends(current_user),
             session: Session = Depends(db, scope="function")):
    """The PipelineSpec checked against its recipes (output contract, joins, reconciliations, incremental
    rules, destination); nothing is stored."""
    require_role(session, user, workspace_id, "analyst")
    out = svc.validate_spec(session, workspace_id, body.spec)
    return {"valid": True, "spec": out["pipeline"].spec(),
            "recipes": {n: {"id": r.id, "version": r.version, "status": r.status} for n, r in out["recipes"].items()}}


@router.post("/workspaces/{workspace_id}/pipelines")
def save(workspace_id: str, body: PipelineIn, user: User = Depends(current_user),
         session: Session = Depends(db, scope="function")):
    return svc.pipeline_view(svc.save_pipeline(session, user, workspace_id, body.spec))


@router.get("/workspaces/{workspace_id}/pipelines")
def list_pipelines(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return [svc.pipeline_view(p) for p in svc.list_pipelines(session, user, workspace_id)]


@router.get("/workspaces/{workspace_id}/pipelines/{pipeline_id}")
def get_pipeline(workspace_id: str, pipeline_id: str, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    return svc.pipeline_view(svc.get_pipeline(session, user, pipeline_id, workspace_id))


@router.post("/workspaces/{workspace_id}/pipelines/{pipeline_id}/publish")
def publish(workspace_id: str, pipeline_id: str, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    return svc.pipeline_view(svc.publish_pipeline(session, user, pipeline_id, workspace_id))


@router.post("/workspaces/{workspace_id}/pipelines/{pipeline_id}/dry-run")
def dry_run(workspace_id: str, pipeline_id: str, body: DryRunIn, user: User = Depends(current_user)):
    """SQL, manifest, checks and the reconciled virtual output; requests the materialize approval when the
    pipeline names a destination and every check passed (status `awaiting_approval`)."""
    return svc.dry_run(user, pipeline_id, workspace_id, engine=_engine(body.engine))


@router.post("/workspaces/{workspace_id}/pipelines/{pipeline_id}/runs")
def run(workspace_id: str, pipeline_id: str, body: RunIn, user: User = Depends(current_user)):
    """Run the recipes into the managed recipe-output source (incremental by watermark when declared)."""
    window = (body.since, body.until) if body.mode in ("replay", "backfill") else None
    return svc.run_pipeline(user, pipeline_id, workspace_id, mode=body.mode, window=window, engine=_engine(body.engine))


@router.get("/workspaces/{workspace_id}/pipeline-runs")
def list_runs(workspace_id: str, pipeline: str | None = None, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    return [svc.run_view(r) for r in svc.list_runs(session, user, workspace_id, pipeline)]


@router.get("/workspaces/{workspace_id}/pipeline-runs/{run_id}")
def get_run(workspace_id: str, run_id: str, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    return svc.run_view(svc.get_run(session, user, run_id, workspace_id))


@router.post("/workspaces/{workspace_id}/pipeline-runs/{run_id}/materialize")
def materialize(workspace_id: str, run_id: str, body: MaterializeIn, user: User = Depends(current_user)):
    """Write the dry run's approved candidate through the managed writer (verified immediately before the
    write). Re-posting after a failure resumes from the checkpoint under the same approval."""
    return svc.materialize(user, run_id, body.approval_id, workspace_id)


@router.get("/workspaces/{workspace_id}/writer-destinations")
def list_destinations(workspace_id: str, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function")):
    return [{k: getattr(d, k) for k in ("id", "engine", "schema_name", "tables", "writer_role", "status", "provisioning",
                                        "created_by", "created_at")}
            for d in svc.list_destinations(session, user, workspace_id)]


@router.post("/workspaces/{workspace_id}/writer-destinations")
def designate(workspace_id: str, body: DestinationIn, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    """Owner only: allowlist a destination schema (and optionally its tables) for the managed writer."""
    d = svc.designate_destination(session, user, workspace_id, body.schema_name, tables=body.tables, engine=body.engine)
    return {k: getattr(d, k) for k in ("id", "engine", "schema_name", "tables", "writer_role", "status", "provisioning")}


@router.get("/workspaces/{workspace_id}/materializations")
def list_materializations(workspace_id: str, table: str | None = None, user: User = Depends(current_user),
                          session: Session = Depends(db, scope="function")):
    return [svc.materialization_view(m) for m in svc.list_materializations(session, user, workspace_id, table)]


@router.post("/workspaces/{workspace_id}/materializations/{materialization_id}/rollback")
def rollback(workspace_id: str, materialization_id: str, user: User = Depends(current_user)):
    """Owner only: re-point the destination at the version this one replaced."""
    return svc.rollback(user, materialization_id, workspace_id)


@router.post("/workspaces/{workspace_id}/sources/{source_id}/refresh")
def refresh(workspace_id: str, source_id: str, body: RefreshIn, user: User = Depends(current_user)):
    """P6-02: full reload, delete reconcile, or replay/backfill of a watermark range for one incremental table."""
    from analystos.services.sources import refresh_asset

    return refresh_asset(user, source_id, body.asset, workspace_id, mode=body.mode, since=body.since, until=body.until)
