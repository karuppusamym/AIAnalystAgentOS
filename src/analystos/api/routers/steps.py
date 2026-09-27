"""Steps, branches, pins and notebooks: the Data Thread API (P7-04, P7-05, P7-12; shapes in
docs/20-contracts/04-brief-steps-api.md). Every child id is bound to the path's workspace (and a step or
branch to its container: a run, a private Ask thread, a notebook) before anything is read or run.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.http import expected_revision, set_etag
from analystos.contracts.step import CellEdit, CellIn, ForkIn, MergeIn, NotebookIn, PinIn, StepEdit, StepIn
from analystos.db.base import session_scope
from analystos.db.models import Notebook, User
from analystos.governance.policy import load_in_workspace
from analystos.services import branches as branch_svc
from analystos.services import notebooks as nb_svc
from analystos.services import step_pins as pin_svc
from analystos.services import steps as steps_svc

router = APIRouter(prefix="/api", tags=["steps"])


# ------------------------------------------------------------------------------------ threads and branches
@router.get("/workspaces/{workspace_id}/threads/{container_type}/{container_id}")
def get_thread(workspace_id: str, container_type: str, container_id: str, user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    """The container's main branch with its steps, and every branch of it."""
    me = session.merge(user)
    steps_svc.load_container(session, me, container_type, container_id, workspace_id)
    main = steps_svc.main_branch(session, workspace_id, container_type, container_id, f"user:{user.id}")
    return {**steps_svc.thread(session, main), "branches": branch_svc.branches(session, container_type, container_id)}


@router.post("/workspaces/{workspace_id}/threads/{container_type}/{container_id}/ingest")
def ingest_thread(workspace_id: str, container_type: str, container_id: str, user: User = Depends(current_user)):
    """Record a run's plan, tested hypotheses and findings, or an Ask thread's answered turns, as steps
    (idempotent; nothing is re-run)."""
    with session_scope() as s:
        steps_svc.load_container(s, s.merge(user), container_type, container_id, workspace_id)
    if container_type == "run":
        return steps_svc.ingest_run(user, workspace_id, container_id)
    if container_type == "ask_thread":
        return steps_svc.ingest_ask_thread(user, workspace_id, container_id)
    from analystos.core.errors import InvalidInput

    raise InvalidInput("only runs and Ask threads are ingested; notebook cells are steps already")


@router.get("/workspaces/{workspace_id}/branches/{branch_id}")
def get_branch(workspace_id: str, branch_id: str, user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    branch = steps_svc.load_branch(session, session.merge(user), branch_id, workspace_id)
    return steps_svc.thread(session, branch)


@router.post("/workspaces/{workspace_id}/branches/{branch_id}/steps", status_code=201)
def add_step(workspace_id: str, branch_id: str, body: StepIn, user: User = Depends(current_user)):
    """Add a step to a branch and run it (SQL through the gateway, self-checked, verdict recorded)."""
    with session_scope() as s:
        steps_svc.load_branch(s, s.merge(user), branch_id, workspace_id, "analyst")
    return steps_svc.create(user, workspace_id, branch_id, body)


@router.post("/workspaces/{workspace_id}/branches/{branch_id}/forks", status_code=201)
def fork_branch(workspace_id: str, branch_id: str, body: ForkIn, user: User = Depends(current_user)):
    with session_scope() as s:
        steps_svc.load_branch(s, s.merge(user), branch_id, workspace_id, "analyst")
        steps_svc.load_step(s, s.merge(user), body.from_step_id, workspace_id)
    return branch_svc.fork(user, workspace_id, branch_id, body)


@router.get("/workspaces/{workspace_id}/branches/{branch_id}/compare")
def compare_branches(workspace_id: str, branch_id: str, other: str = Query(..., alias="with"),
                     user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Two branches side by side: steps paired by lineage, with spec diffs, numeric deltas and both verdicts."""
    me = session.merge(user)
    a = steps_svc.load_branch(session, me, branch_id, workspace_id)
    b = steps_svc.load_branch(session, me, other, workspace_id)
    return branch_svc.compare(session, a, b)


@router.post("/workspaces/{workspace_id}/branches/{branch_id}/merge")
def merge_branch(workspace_id: str, branch_id: str, body: MergeIn, user: User = Depends(current_user)):
    """Merge a branch into a (new or earlier) Data Thread report; a void verdict refuses the merge."""
    with session_scope() as s:
        steps_svc.load_branch(s, s.merge(user), branch_id, workspace_id, "analyst")
        if body.report_id:
            from analystos.db.models import Artifact

            load_in_workspace(s, Artifact, body.report_id, workspace_id, user=s.merge(user), minimum="analyst", label="report")
    return branch_svc.merge(user, workspace_id, branch_id, body)


# ------------------------------------------------------------------------------------ steps
@router.get("/workspaces/{workspace_id}/steps/{step_id}")
def get_step(workspace_id: str, step_id: str, response: Response, version: int | None = None,
             user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """One version (default: current) with its result snapshot. ETag = the current version."""
    step = steps_svc.load_step(session, session.merge(user), step_id, workspace_id)
    set_etag(response, step.current_version)
    return steps_svc.with_result(session, step, version)


@router.get("/workspaces/{workspace_id}/steps/{step_id}/why")
def why_step_number(workspace_id: str, step_id: str, version: int | None = None, number: str | None = None,
                    column: str | None = None, row: int | None = None, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    """"Why this number?" (P7-08) for a step version (default: current): each numeric cell (or the one given by
    `number`, `column` and/or `row`) resolved fact -> step -> query receipt -> data version -> semantic version ->
    verdict, every link with its current state; missing and voided links are returned, never dropped."""
    from analystos.evidence.why import explain_step

    step = steps_svc.load_step(session, session.merge(user), step_id, workspace_id)
    return explain_step(session, step, version=version, number=number, column=column, row=row)


@router.get("/workspaces/{workspace_id}/steps/{step_id}/versions")
def step_versions(workspace_id: str, step_id: str, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    step = steps_svc.load_step(session, session.merge(user), step_id, workspace_id)
    return {"step_id": step.id, "current_version": step.current_version, "versions": steps_svc.versions(session, step)}


@router.patch("/workspaces/{workspace_id}/steps/{step_id}")
def edit_step(workspace_id: str, step_id: str, body: StepEdit, response: Response, user: User = Depends(current_user),
              if_match: str | None = Header(default=None)):
    """Edit: version + 1, re-run it and its dependents; earlier verdicts turn VOID. If-Match is required."""
    expected = expected_revision(if_match, required=True)
    with session_scope() as s:
        steps_svc.load_step(s, s.merge(user), step_id, workspace_id, "analyst")
    out = steps_svc.edit(user, step_id, body, expected_version=expected)
    set_etag(response, out["step"]["current_version"])
    return out


@router.post("/workspaces/{workspace_id}/steps/{step_id}/runs")
def rerun_step(workspace_id: str, step_id: str, user: User = Depends(current_user)):
    with session_scope() as s:
        steps_svc.load_step(s, s.merge(user), step_id, workspace_id, "analyst")
    return steps_svc.rerun(user, step_id)


@router.post("/workspaces/{workspace_id}/steps/{step_id}/pins")
def pin_step(workspace_id: str, step_id: str, body: PinIn, user: User = Depends(current_user),
             session: Session = Depends(db, scope="function")):
    """Pin a verified step version to a tile or a schedule: 202 with the approval to request, then (with the
    approved `approval_id`) 201 with the pin."""
    me = session.merge(user)
    step = steps_svc.load_step(session, me, step_id, workspace_id, "editor")
    out = pin_svc.pin(session, me, step, body)
    return JSONResponse(status_code=202 if out["status"] == "approval_required" else 201, content=out)


@router.get("/workspaces/{workspace_id}/pins/{pin_id}")
def get_pin(workspace_id: str, pin_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    p = pin_svc.load_pin(session, session.merge(user), pin_id, workspace_id)
    return {**pin_svc.pin_view(p), "state": pin_svc.state(session, p)}


@router.post("/workspaces/{workspace_id}/pins/{pin_id}/refresh")
def refresh_pin(workspace_id: str, pin_id: str, user: User = Depends(current_user)):
    """Replay the pin's frozen query on today's data (a tile refresh)."""
    with session_scope() as s:
        pin_svc.load_pin(s, s.merge(user), pin_id, workspace_id, "analyst")
    return pin_svc.replay(user, workspace_id, pin_id)


# ------------------------------------------------------------------------------------ notebooks
@router.post("/workspaces/{workspace_id}/notebooks", status_code=201)
def create_notebook(workspace_id: str, body: NotebookIn, user: User = Depends(current_user),
                    session: Session = Depends(db, scope="function")):
    return nb_svc.create(session, session.merge(user), workspace_id, body)


@router.get("/workspaces/{workspace_id}/notebooks")
def list_notebooks(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return nb_svc.list_notebooks(session, session.merge(user), workspace_id)


@router.get("/workspaces/{workspace_id}/notebooks/{notebook_id}")
def get_notebook(workspace_id: str, notebook_id: str, response: Response, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    nb = load_in_workspace(session, Notebook, notebook_id, workspace_id, user=session.merge(user), label="notebook")
    set_etag(response, nb.revision)
    return nb_svc.notebook_view(session, nb)


@router.post("/workspaces/{workspace_id}/notebooks/{notebook_id}/cells", status_code=201)
def add_cell(workspace_id: str, notebook_id: str, body: CellIn, user: User = Depends(current_user)):
    """Add a cell and run it: SQL through the gateway, Python in the sandbox on earlier cells' results."""
    with session_scope() as s:
        load_in_workspace(s, Notebook, notebook_id, workspace_id, user=s.merge(user), minimum="analyst", label="notebook")
    return nb_svc.add_cell(user, workspace_id, notebook_id, body)


@router.patch("/workspaces/{workspace_id}/notebooks/{notebook_id}/cells/{step_id}")
def edit_cell(workspace_id: str, notebook_id: str, step_id: str, body: CellEdit, response: Response,
              user: User = Depends(current_user), if_match: str | None = Header(default=None)):
    """Edit a cell: a new version, re-run with the cells that read it (their verdicts void). If-Match required."""
    expected = expected_revision(if_match, required=True)
    with session_scope() as s:
        load_in_workspace(s, Notebook, notebook_id, workspace_id, user=s.merge(user), minimum="analyst", label="notebook")
        step = steps_svc.load_step(s, s.merge(user), step_id, workspace_id, "analyst")
        if step.container_id != notebook_id:
            from analystos.core.errors import NotFound

            raise NotFound("cell not found")
    out = nb_svc.edit_cell(user, step_id, body, expected_version=expected)
    set_etag(response, out["step"]["current_version"])
    return out


@router.post("/workspaces/{workspace_id}/notebooks/{notebook_id}/runs")
def run_notebook(workspace_id: str, notebook_id: str, user: User = Depends(current_user)):
    with session_scope() as s:
        load_in_workspace(s, Notebook, notebook_id, workspace_id, user=s.merge(user), minimum="analyst", label="notebook")
    return nb_svc.run_all(user, workspace_id, notebook_id)
