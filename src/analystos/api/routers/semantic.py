"""Workspace semantic layer API (P4-K03): the Ossie model, the metric approval workflow, conflicts,
dbt import and dbt / Superset export; review (P7-02, P7-09, P4-05): diffs before approval, structure
approvals, reconciliation, the caller's governed catalog and the relationship review queue."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.contracts.semantic import MetricProposalIn
from analystos.core.errors import InvalidInput
from analystos.db.models import SemanticRelationshipCandidate, User
from analystos.governance.policy import load_in_workspace, require_role
from analystos.semantic import ossie, review
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
def get_model(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
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
                   session: Session = Depends(db, scope="function")):
    """The workspace model as an Apache Ossie 0.1.1 document (YAML download, or JSON)."""
    require_role(session, user, workspace_id, "viewer")
    doc = svc.ossie_document(session, workspace_id, include=_include(include))
    if format == "json":
        return doc
    return Response(ossie.dump_yaml(doc), media_type="application/yaml",
                    headers={"Content-Disposition": 'attachment; filename="semantic_model.ossie.yaml"'})


@router.get("/metrics")
def list_metrics(workspace_id: str, status: str | None = None, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return rows(svc.metric_rows(session, workspace_id, status=status))


@router.get("/metrics/{name}")
def metric_versions(workspace_id: str, name: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return rows(svc.metric_rows(session, workspace_id, name=name))


@router.post("/metrics")
def propose(workspace_id: str, body: MetricProposalIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "editor")
    r, created = svc.propose_from_input(session, workspace_id, body, user)
    return {"metric": row(r), "created": created,
            "conflicts": [c.model_dump() for c in svc.conflicts(session, workspace_id) if body.name in c.names]}


@router.post("/metrics/validate")
def validate(workspace_id: str, body: MetricProposalIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Check a proposal without recording it (the KPI editor's live validation): field problems and the
    conflicts it would create. Proposing still re-checks everything."""
    require_role(session, user, workspace_id, "viewer")
    return svc.validate_proposal(session, workspace_id, body)


@router.post("/metrics/{name}/approve")
def approve(workspace_id: str, name: str, body: Decision | None = None, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "approver")
    body = body or Decision()
    return row(svc.decide_metric(session, workspace_id, name, session.merge(user), approve=True, version=body.version,
                                 reason=body.reason))


@router.post("/metrics/{name}/reject")
def reject(workspace_id: str, name: str, body: Decision | None = None, user: User = Depends(current_user),
           session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "approver")
    body = body or Decision()
    return row(svc.decide_metric(session, workspace_id, name, session.merge(user), approve=False, version=body.version,
                                 reason=body.reason))


@router.post("/metrics/{name}/deprecate")
def deprecate(workspace_id: str, name: str, body: Decision | None = None, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "approver")
    return rows(svc.deprecate_metric(session, workspace_id, name, session.merge(user), reason=(body or Decision()).reason))


@router.get("/conflicts")
def list_conflicts(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return [c.model_dump() for c in svc.conflicts(session, workspace_id)]


@router.post("/import/dbt")
def import_dbt(workspace_id: str, document: dict = Body(...), user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Body: the content of dbt's `target/osi_document.json`. Metrics arrive as proposals."""
    require_role(session, user, workspace_id, "editor")
    return svc.import_dbt(session, workspace_id, document, session.merge(user))


@router.post("/import/ossie")
def import_ossie(workspace_id: str, document: str = Body(..., embed=True), user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    """Body: {"document": "<an Ossie 0.1.1 document, YAML or JSON>"}. Metrics arrive as proposals."""
    require_role(session, user, workspace_id, "editor")
    return svc.import_ossie(session, workspace_id, document, session.merge(user))


@router.get("/export/dbt")
def export_dbt(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """`osi/*.json` files for a dbt 1.12 project (approved metrics only) and what dbt would object to."""
    require_role(session, user, workspace_id, "viewer")
    return svc.export_dbt(session, workspace_id)


@router.get("/export/superset")
def export_superset(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Approved metrics as Superset dataset-metric payloads (what the Superset publisher writes)."""
    require_role(session, user, workspace_id, "viewer")
    return svc.export_superset(session, workspace_id)


# ---------------------------------------------------------------------------------- review (P7-02, P7-09, P4-05)
class ModelDecision(BaseModel):
    version: int
    reason: str | None = None


class DiscoverIn(BaseModel):
    assets: list[str] | None = None  # "schema.table" in the caller's scope; None = every selected asset
    composite: bool = True


class RelationshipProposalIn(BaseModel):
    """Columns only: a cardinality is measured through the gateway, never taken from a request."""

    model_config = ConfigDict(extra="forbid")
    from_asset: str
    from_columns: list[str] = Field(min_length=1, max_length=3)
    to_asset: str
    to_columns: list[str] = Field(min_length=1, max_length=3)


@router.get("/metrics/{name}/diff")
def metric_diff(workspace_id: str, name: str, version: int | None = None, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    """The field-level diff of a metric version against the approved one (shown before approval)."""
    require_role(session, user, workspace_id, "viewer")
    return svc.metric_diff(session, workspace_id, name, version)


@router.get("/model/diff")
def model_diff(workspace_id: str, version: int | None = None, user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return review.model_diff(session, workspace_id, version)


@router.post("/model/approve")
def approve_model(workspace_id: str, body: ModelDecision, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    """Approve a proposed structure version (entities, grain, joins): separation of duties, hash-bound."""
    require_role(session, user, workspace_id, "approver")
    return row(review.decide_model(session, workspace_id, body.version, session.merge(user), approve=True, reason=body.reason))


@router.post("/model/reject")
def reject_model(workspace_id: str, body: ModelDecision, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "approver")
    return row(review.decide_model(session, workspace_id, body.version, session.merge(user), approve=False, reason=body.reason))


@router.get("/reconciliation")
def reconciliation(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Fan-out, denominator, stale-definition and unvalidated-join findings (P4-05)."""
    require_role(session, user, workspace_id, "viewer")
    return review.reconcile(session, workspace_id)


@router.get("/catalog")
def governed_catalog(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """What the caller's governed planner sees: approved metrics and fields, masked fields absent."""
    from analystos.governance.policy import resolve_scope
    from analystos.semantic.compiler import COMPILER_VERSION, load_catalog, planner_catalog

    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="viewer")
    catalog = load_catalog(session, workspace_id)
    if catalog is None:
        return {"model_version": None, "compiler_version": COMPILER_VERSION, "metrics": {}, "datasets": []}
    seen = planner_catalog(catalog, scope)
    return {"model_version": seen["version"], "compiler_version": COMPILER_VERSION, "metrics": seen["metrics"],
            "datasets": seen["datasets"], "relationships": seen["relationships"], "synonyms": seen["synonyms"]}


@router.post("/relationships/discover")
def discover_relationships(workspace_id: str, body: DiscoverIn | None = None, user: User = Depends(current_user),
                           session: Session = Depends(db, scope="function")):
    """Measure relationship candidates (single-column and composite) through the gateway, as the caller,
    and queue them for review (P7-09)."""
    require_role(session, user, workspace_id, "editor")
    body = body or DiscoverIn()
    return rows(review.discover_candidates(session, session.merge(user), workspace_id, assets=body.assets,
                                           composite=body.composite))


@router.post("/relationships/candidates")
def propose_relationship(workspace_id: str, body: RelationshipProposalIn, user: User = Depends(current_user),
                         session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "editor")
    return row(review.propose_candidate(session, session.merge(user), workspace_id, **body.model_dump()))


@router.get("/relationships/candidates")
def relationship_candidates(workspace_id: str, status: str | None = None, user: User = Depends(current_user),
                            session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return rows(review.candidates(session, workspace_id, status))


@router.post("/relationships/candidates/{candidate_id}/accept")
def accept_relationship(workspace_id: str, candidate_id: str, body: Decision | None = None, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    """Accept a measured candidate: separation of duties, hash-bound to the measurement; writes the
    relationship with its measured cardinality and validated_at/validated_by."""
    load_in_workspace(session, SemanticRelationshipCandidate, candidate_id, workspace_id, user=user, minimum="approver",
                      label="relationship candidate")
    return row(review.decide_candidate(session, workspace_id, candidate_id, session.merge(user), approve=True,
                                       reason=(body or Decision()).reason))


@router.post("/relationships/candidates/{candidate_id}/reject")
def reject_relationship(workspace_id: str, candidate_id: str, body: Decision | None = None, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    load_in_workspace(session, SemanticRelationshipCandidate, candidate_id, workspace_id, user=user, minimum="approver",
                      label="relationship candidate")
    return row(review.decide_candidate(session, workspace_id, candidate_id, session.merge(user), approve=False,
                                       reason=(body or Decision()).reason))


@router.post("/relationships/candidates/{candidate_id}/measure")
def remeasure_relationship(workspace_id: str, candidate_id: str, user: User = Depends(current_user),
                           session: Session = Depends(db, scope="function")):
    """Measure a pending candidate again (a decision needs a measurement younger than 7 days)."""
    cand = load_in_workspace(session, SemanticRelationshipCandidate, candidate_id, workspace_id, user=user, minimum="editor",
                             label="relationship candidate")
    if cand.status != "pending":
        raise InvalidInput(f"relationship candidate is {cand.status}")
    return row(review.propose_candidate(session, session.merge(user), workspace_id, from_asset=cand.from_asset,
                                        from_columns=cand.from_columns, to_asset=cand.to_asset, to_columns=cand.to_columns,
                                        origin=cand.origin if cand.origin != "discovered" else "user"))
