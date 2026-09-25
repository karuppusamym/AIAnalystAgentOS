"""The MVP flow (spec v1 §62) on the local orchestrator with no model provider: every agent must
degrade to its labelled deterministic path and still produce verified, evidence-backed output.
Publishes to the preview destination when Superset is unreachable (as in CI)."""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import select

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def servicenow_url():
    from analystos.connectors.servicenow_mock import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def _wait(run_id: str, statuses: set[str], timeout: float = 600) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        if status in statuses:
            return status
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def test_full_run_without_models(control_db, servicenow_url, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SERVICENOW_PASSWORD", "admin")
    monkeypatch.setenv("ANALYSTOS_SUPERSET_URL", "http://127.0.0.1:9")  # force the preview destination
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    get_settings.cache_clear()
    default_router.cache_clear()
    try:
        from analystos.db.base import session_scope
        from analystos.db.models import AnalysisRun, Approval, Artifact, Insight, QueryExecution, User
        from analystos.governance.approvals import decide
        from analystos.services.runs import create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import add_member, create_workspace

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="e2e local", objective="Find the drivers of SLA breaches in IT incidents")
            s.flush()
            add_member(s, admin, ws.id, "approver@analystos.local", "approver")
            src = register_source(s, admin, ws.id, kind="servicenow", name="SN",
                                  config={"instance_url": servicenow_url, "username": "admin", "tables": ["incident", "change_request"]},
                                  secret_ref="env:SERVICENOW_PASSWORD")
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["incident", "change_request"])
        run = create_run(admin, ws_id, objective=None)
        assert _wait(run.id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"

        with session_scope() as s:
            approval = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending"))
            assert approval is not None and approval.destination == "preview"
            approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
            decide(s, approval.id, approver, approve=True)
        from analystos.workflows.orchestrator import run_local

        assert run_local(run.id) == "COMPLETED"

        with session_scope() as s:
            r = s.get(AnalysisRun, run.id)
            verified = list(s.scalars(select(Insight).where(Insight.run_id == run.id, Insight.status == "verified")))
            arts = list(s.scalars(select(Artifact).where(Artifact.run_id == run.id)))
            queries = s.scalar(select(QueryExecution.id).where(QueryExecution.run_id == run.id).limit(1))
            by_type: dict[str, list] = {}
            for a in arts:
                by_type.setdefault(a.type, []).append(a)
            assert len(verified) >= 3, [i.title for i in verified]
            assert all(i.verification["verify"]["reproducible"] for i in verified)
            assert all(any(e["type"] == "query" for e in i.evidence) for i in verified)
            assert by_type.get("dataset")
            assert len(by_type.get("metric", [])) >= 3
            assert len(by_type.get("chart", [])) >= 5
            assert {a.name for a in by_type.get("dashboard", [])} == {"executive", "operational"}
            assert all(a.status == "published" and a.platform == "preview" for a in by_type["dashboard"])
            assert queries is not None
            assert r.summary.get("published") is True
    finally:
        get_settings.cache_clear()
        default_router.cache_clear()
