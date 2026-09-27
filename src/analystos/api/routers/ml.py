"""Governed ML API (P5-01..P5-06, ADR-0024). Every child id is bound to the path's workspace through a
scoped loader. Specs are `ml_spec` definitions and scoring jobs `ml_scoring` definitions, authored through
the generic definitions API (`/api/workspaces/{id}/definitions`) and published before they run.
Reference: docs/30-runbooks/ml-api.md."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.models import SourceAsset, SourceColumn, User
from analystos.governance.policy import require_role, resolve_scope
from analystos.services import ml as ml_svc

router = APIRouter(prefix="/api", tags=["ml"])


class ExperimentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: dict[str, Any] | str  # {key, version} of a published ml_spec, or a definition id
    max_trials: int | None = Field(default=None, ge=1, le=1000)  # can only tighten the spec and platform caps
    max_seconds: int | None = Field(default=None, ge=1)


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str | None = None


class ScoringIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: dict[str, Any] | str  # {key, version} of a published ml_scoring definition, or its id


class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: str
    objective: str | None = None
    target: str | None = None
    task: Literal["classify", "regress", "forecast", "cluster", "anomaly"] | None = None
    features: list[str] | None = None
    estimators: list[str] | None = None
    time_column: str | None = None
    horizon: int | None = Field(default=None, ge=1, le=366)


# ------------------------------------------------------------------------------------ proposals
@router.post("/workspaces/{workspace_id}/ml/proposals")
def propose(workspace_id: str, body: ProposalIn, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    """A rules-first MLSpec proposal from the catalog (no model call, nothing stored or trained)."""
    from analystos.ml.propose import propose as rules

    scope = resolve_scope(session, user, workspace_id, minimum_role="analyst")
    if body.asset not in scope.assets:
        return {"proposal": None, "problems": [f"{body.asset} is not in the authorized scope"], "source": "inputs"}
    schema, name = body.asset.split(".", 1)
    asset = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema,
                                                     SourceAsset.name == name).limit(1))
    cols = list(session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal))) \
        if asset else []
    visible = [{"name": c.name, "data_type": c.data_type, "semantic_type": c.semantic_type, "is_key": bool(c.is_key),
                "distinct": (c.profile or {}).get("distinct")} for c in cols
               if f"{body.asset}.{c.name}" not in set(scope.denied_columns)]
    return rules(visible, row_count=asset.row_count if asset else None, **body.model_dump())


# ------------------------------------------------------------------------------------ experiments
@router.post("/workspaces/{workspace_id}/ml/experiments", status_code=201)
def start_experiment(workspace_id: str, body: ExperimentIn, user: User = Depends(current_user)):
    """Readiness and leakage checks, split manifest, baseline + bounded search, one holdout read, sealed
    evaluation, package, model card and verification record. A refusal is a 422 naming the experiment."""
    return ml_svc.start_experiment(user, workspace_id, body.definition,
                                   caps={"max_trials": body.max_trials, "max_seconds": body.max_seconds})


@router.get("/workspaces/{workspace_id}/ml/experiments")
def list_experiments(workspace_id: str, definition: str | None = None, user: User = Depends(current_user),
                     session: Session = Depends(db, scope="function")):
    return [ml_svc.experiment_view(e) for e in ml_svc.list_experiments(session, user, workspace_id, definition)]


@router.get("/workspaces/{workspace_id}/ml/experiments/{experiment_id}")
def get_experiment(workspace_id: str, experiment_id: str, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    exp = ml_svc.get_experiment(session, user, experiment_id, workspace_id)
    return {**ml_svc.experiment_view(exp), "verification": ml_svc.verification_of(session, exp)}


@router.get("/workspaces/{workspace_id}/ml/experiments/{experiment_id}/records/{record}")
def experiment_record(workspace_id: str, experiment_id: str, record: str, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function")):
    """`record`: ml_spec | ml_split_manifest | ml_trials | ml_model | ml_evaluation | ml_model_card."""
    return ml_svc.experiment_record(session, user, experiment_id, record, workspace_id)


@router.get("/workspaces/{workspace_id}/ml/experiments/{experiment_id}/mlflow")
def export_mlflow(workspace_id: str, experiment_id: str, user: User = Depends(current_user)):
    """The experiment as an MLflow file store (`mlruns/`), zipped."""
    name, data = ml_svc.export_mlflow(user, experiment_id, workspace_id)
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------------------------ registry
@router.get("/workspaces/{workspace_id}/ml/models")
def list_models(workspace_id: str, name: str | None = None, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    return [ml_svc.version_view(v) for v in ml_svc.list_versions(session, user, workspace_id, name)]


@router.post("/workspaces/{workspace_id}/ml/models/{version_id}/promote")
def promote(workspace_id: str, version_id: str, body: ApprovalIn, user: User = Depends(current_user)):
    """Without approval_id: request the hash-bound `ml.promote` approval. With it (approved by an approver):
    make the version the champion; the replaced champion is kept as the rollback version."""
    return ml_svc.promote(user, version_id, workspace_id, approval_id=body.approval_id)


@router.post("/workspaces/{workspace_id}/ml/model-names/{name}/rollback")
def rollback(workspace_id: str, name: str, body: ApprovalIn, user: User = Depends(current_user),
             session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "editor")
    return ml_svc.rollback(user, workspace_id, name, approval_id=body.approval_id)


# ------------------------------------------------------------------------------------ scoring
@router.post("/workspaces/{workspace_id}/ml/scoring")
def plan_scoring(workspace_id: str, body: ScoringIn, user: User = Depends(current_user)):
    """Plan an approved batch scoring of a published, pinned `ml_scoring` definition: champion and feature
    parity checks, input snapshot, `ml.score` approval (or `duplicate` when this input was already scored)."""
    return ml_svc.plan_scoring(user, workspace_id, body.definition)


@router.post("/workspaces/{workspace_id}/ml/scoring/{scoring_id}/execute")
def execute_scoring(workspace_id: str, scoring_id: str, body: ApprovalIn, user: User = Depends(current_user)):
    return ml_svc.execute_scoring(user, scoring_id, workspace_id, approval_id=body.approval_id)


@router.get("/workspaces/{workspace_id}/ml/scoring")
def list_scoring(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return [ml_svc.scoring_view(r) for r in ml_svc.list_scoring(session, user, workspace_id)]


@router.get("/workspaces/{workspace_id}/ml/scoring/{scoring_id}")
def get_scoring(workspace_id: str, scoring_id: str, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    return ml_svc.scoring_view(ml_svc.get_scoring(session, user, scoring_id, workspace_id))
