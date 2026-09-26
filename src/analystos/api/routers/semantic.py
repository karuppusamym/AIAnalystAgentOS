"""Workspace semantic layer API (P4-K03): the Ossie model, the metric approval workflow, conflicts,
dbt import and dbt / Superset export."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.contracts.semantic import MetricProposalIn
from analystos.core.errors import InvalidInput
from analystos.db.models import User
from analystos.governance.policy import require_role
from analystos.semantic import ossie
from analystos.semantic import service as svc

router = APIRouter(prefix="/api/workspaces/{workspace_id}/semantic", tags=["semantic"])


class Decision(BaseModel):
    version: int | None = None
    reason: str | None = None


def _include(include: str) -> str:
    if include not in ("approved", "all"):
        raise InvalidInput("include must be 'approved' or 'all'")
    return include


@router.get("")
def get_model(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    cur = svc.current_model(session, workspace_id)
    latest: dict[str, dict] = {}
    for r in svc.metric_rows(session, workspace_id):
        latest[r.name] = row(r)
    return {"model": row(cur) if cur else None, "metrics": list(latest.values()),
            "approved": sorted(svc.approved_metrics(session, workspace_id)),
            "conflicts": [c.model_dump() for c in svc.conflicts(session, workspace_id)],
            "ossie_version": ossie.OSSIE_VERSION}


@router.get("/ossie")
def download_ossie(workspace_id: str, include: str = "approved", format: str = "yaml", user: User = Depends(current_user),
                   session: Session = Depends(db)):
    """The workspace model as an Apache Ossie 0.1.1 document (YAML download, or JSON)."""
    require_role(session, user, workspace_id, "viewer")
    doc = svc.ossie_document(session, workspace_id, include=_include(include))
    if format == "json":
        return doc
    return Response(ossie.dump_yaml(doc), media_type="application/yaml",
                    headers={"Content-Disposition": 'attachment; filename="semantic_model.ossie.yaml"'})


@router.get("/metrics")
def list_metrics(workspace_id: str, status: str | None = None, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    return rows(svc.metric_rows(session, workspace_id, status=status))


@router.get("/metrics/{name}")
def metric_versions(workspace_id: str, name: str, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    return rows(svc.metric_rows(session, workspace_id, name=name))


@router.post("/metrics")
def propose(workspace_id: str, body: MetricProposalIn, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "editor")
    r, created = svc.propose_from_input(session, workspace_id, body, user)
    return {"metric": row(r), "created": created,
            "conflicts": [c.model_dump() for c in svc.conflicts(session, workspace_id) if body.name in c.names]}


@router.post("/metrics/validate")
def validate(workspace_id: str, body: MetricProposalIn, user: User = Depends(current_user), session: Session = Depends(db)):
    """Check a proposal without recording it (the KPI editor's live validation): field problems and the
    conflicts it would create. Proposing still re-checks everything."""
    require_role(session, user, workspace_id, "viewer")
    return svc.validate_proposal(session, workspace_id, body)


@router.post("/metrics/{name}/approve")
def approve(workspace_id: str, name: str, body: Decision | None = None, user: User = Depends(current_user),
            session: Session = Depends(db)):
    require_role(session, user, workspace_id, "approver")
    body = body or Decision()
    return row(svc.decide_metric(session, workspace_id, name, session.merge(user), approve=True, version=body.version,
                                 reason=body.reason))


@router.post("/metrics/{name}/reject")
def reject(workspace_id: str, name: str, body: Decision | None = None, user: User = Depends(current_user),
           session: Session = Depends(db)):
    require_role(session, user, workspace_id, "approver")
    body = body or Decision()
    return row(svc.decide_metric(session, workspace_id, name, session.merge(user), approve=False, version=body.version,
                                 reason=body.reason))


@router.post("/metrics/{name}/deprecate")
def deprecate(workspace_id: str, name: str, body: Decision | None = None, user: User = Depends(current_user),
              session: Session = Depends(db)):
    require_role(session, user, workspace_id, "approver")
    return rows(svc.deprecate_metric(session, workspace_id, name, session.merge(user), reason=(body or Decision()).reason))


@router.get("/conflicts")
def list_conflicts(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    return [c.model_dump() for c in svc.conflicts(session, workspace_id)]


@router.post("/import/dbt")
def import_dbt(workspace_id: str, document: dict = Body(...), user: User = Depends(current_user), session: Session = Depends(db)):
    """Body: the content of dbt's `target/osi_document.json`. Metrics arrive as proposals."""
    require_role(session, user, workspace_id, "editor")
    return svc.import_dbt(session, workspace_id, document, session.merge(user))


@router.post("/import/ossie")
def import_ossie(workspace_id: str, document: str = Body(..., embed=True), user: User = Depends(current_user),
                 session: Session = Depends(db)):
    """Body: {"document": "<an Ossie 0.1.1 document, YAML or JSON>"}. Metrics arrive as proposals."""
    require_role(session, user, workspace_id, "editor")
    return svc.import_ossie(session, workspace_id, document, session.merge(user))


@router.get("/export/dbt")
def export_dbt(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    """`osi/*.json` files for a dbt 1.12 project (approved metrics only) and what dbt would object to."""
    require_role(session, user, workspace_id, "viewer")
    return svc.export_dbt(session, workspace_id)


@router.get("/export/superset")
def export_superset(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    """Approved metrics as Superset dataset-metric payloads (what the Superset publisher writes)."""
    require_role(session, user, workspace_id, "viewer")
    return svc.export_superset(session, workspace_id)
