"""Evidence in open formats (P4-K04): a finding as an OKF Attested Computation, the OpenLineage events
of a run's governed queries, and the ODCS contracts of published datasets.

Reads only: each route returns data the caller's workspace role may already see. Writing findings
into the workspace pack is an in-platform knowledge draft, not an external side effect."""
from __future__ import annotations

import yaml
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.db.models import AnalysisRun, Insight, User
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader

router = APIRouter(prefix="/api", tags=["evidence"])


@scoped_loader
def _run(session: Session, user: User, workspace_id: str, run_id: str, minimum: str = "viewer") -> AnalysisRun:
    return load_in_workspace(session, AnalysisRun, run_id, workspace_id, user=user, minimum=minimum, label="run")


@router.get("/insights/{insight_id}/attested")
def attested(insight_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The verified finding as an OKF v0.2 Attested Computation (document text and frontmatter)."""
    from analystos.knowledge.attested import attested_from_insight

    ins = load_in_workspace(session, Insight, insight_id, user=user, label="insight")
    ac = attested_from_insight(session, ins)
    return {"path": ac.path, "frontmatter": ac.frontmatter(), "document": ac.render()}


@router.get("/insights/{insight_id}/why")
def why_number(insight_id: str, number: str | None = None, fact_id: str | None = None, user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    """"Why this number?" (P7-08): each displayed number of the finding (or the one given as `number` text
    or `fact_id`) resolved fact -> step -> query receipt -> data version -> semantic version -> verdict,
    every link with its current state; broken and voided links are returned, never dropped."""
    from analystos.evidence.why import explain_insight

    ins = load_in_workspace(session, Insight, insight_id, user=user, label="insight")
    return explain_insight(session, ins, number=number, fact_id=fact_id)


@router.get("/workspaces/{workspace_id}/analysis/{run_id}/why")
def why_run(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Every number of the run's reported findings resolved as in `/insights/{id}/why`, with counts by state."""
    from analystos.evidence.why import explain_run

    _run(session, user, workspace_id, run_id)
    return explain_run(session, run_id)


@router.post("/workspaces/{workspace_id}/analysis/{run_id}/findings/attest")
def attest_run_findings(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Write the run's verified findings into the workspace pack as draft Attested Computations."""
    from analystos.governance.audit import audit
    from analystos.knowledge.attested import write_findings

    _run(session, user, workspace_id, run_id, "editor")
    out = write_findings(session, run_id, author=f"human:{user.id}")
    audit(f"user:{user.id}", "knowledge.findings_attested", workspace_id=workspace_id, run_id=run_id, target=run_id,
          details={"written": out["written"], "kept_curated": out["kept_curated"], "revision": out["revision"]}, session=session)
    return out


@router.get("/workspaces/{workspace_id}/analysis/{run_id}/openlineage")
def run_openlineage(workspace_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """OpenLineage RunEvents (START and COMPLETE/FAIL) for every governed query of the run."""
    from analystos.evidence.openlineage import events_for_run

    _run(session, user, workspace_id, run_id)
    return events_for_run(session, run_id, workspace_id=workspace_id)


@router.get("/workspaces/{workspace_id}/contracts")
def data_contracts(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The ODCS v3.2 contracts of the workspace's published datasets (from its knowledge pack)."""
    from analystos.knowledge import store

    require_role(session, user, workspace_id, "viewer")
    pack = store.workspace_pack(session, workspace_id, create=False)
    if pack is None:
        return []
    out = []
    for path, data in store.revision_files(session, pack).items():
        if path.startswith("contracts/") and path.endswith(".odcs.yaml"):
            doc = yaml.safe_load(data) or {}
            out.append({"path": path, "id": doc.get("id"), "name": doc.get("name"), "version": doc.get("version"),
                        "contract": doc})
    return out
