"""P4-T07 against Postgres + Redis: budget checks on the hot path read Redis counters, not
`COUNT(*)` over query_execution or `SUM` over model_call; the database is the seed and the fallback.
P4-T01: /api/admin/token-savings breaks spend and savings down by purpose and rung."""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from analystos.core.ids import new_id

pytestmark = pytest.mark.integration


@pytest.fixture
def statements():
    """Every SQL statement any engine executes while the fixture is active."""
    from sqlalchemy.engine import Engine

    seen: list[str] = []

    def listen(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", listen)
    yield seen
    event.remove(Engine, "before_cursor_execute", listen)


@pytest.fixture
def counters(monkeypatch):
    """Real Redis, a key prefix of this test only."""
    from analystos.runtime import budget_counters as bc

    c = bc.BudgetCounters("redis://localhost:6379/0", f"aostest:{new_id('b')}:")
    if not c.available:
        pytest.skip("Redis unavailable")
    monkeypatch.setattr(bc, "default_budget_counters", lambda: c)
    return c


def _count_query_execution(sql: list[str]) -> int:
    return sum(1 for s in sql if re.search(r"count\(", s, re.I) and "query_execution" in s)


def _sum_model_call(sql: list[str]) -> int:
    return sum(1 for s in sql if re.search(r"sum\(", s, re.I) and "model_call" in s)


def _run_ctx(run_id: str, budget: int):
    from analystos.contracts.policy import WorkspacePolicyDoc
    from analystos.runtime.context import RunContext

    return RunContext(run=SimpleNamespace(id=run_id), task=None, user=None, workspace=None,
                      policy=WorkspacePolicyDoc(max_queries_per_run=budget), scope=None, agent=None, services=None)


def _workspace(policy: dict | None = None) -> str:
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.workspaces import create_workspace

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name=f"budgets {new_id('w')}", policy=policy)
        s.flush()
        return ws.id


def test_run_query_budget_has_no_count_on_the_hot_path(control_db, counters, statements):
    from analystos.core.errors import BudgetExceeded
    from analystos.db.base import session_scope
    from analystos.db.models import QueryExecution

    run_id = new_id("run")
    with session_scope() as s:  # two statements already ran (e.g. before a worker restart)
        for _ in range(2):
            s.add(QueryExecution(id=new_id("q"), workspace_id="ws_x", run_id=run_id, actor="agent:t", sql="SELECT 1", status="ok"))
    ctx = _run_ctx(run_id, budget=25)
    statements.clear()
    ctx.check_query_budget()
    assert _count_query_execution(statements) == 1  # the one-time seed of a missing counter
    statements.clear()
    for _ in range(22):
        ctx.check_query_budget()
    assert _count_query_execution(statements) == 0 and statements == []  # hot path: Redis only
    with pytest.raises(BudgetExceeded, match=r"per-run query budget \(25\)"):
        ctx.check_query_budget()  # 2 seeded + 23 counted = 25 used


def test_run_query_budget_falls_back_to_the_database_when_redis_is_down(control_db, monkeypatch, statements, caplog):
    from analystos.runtime import budget_counters as bc

    with caplog.at_level(logging.WARNING):
        down = bc.BudgetCounters("redis://127.0.0.1:1/0", "aostest:down:")
    monkeypatch.setattr(bc, "default_budget_counters", lambda: down)
    assert "budgets fall back to database aggregates" in caplog.text
    ctx = _run_ctx(new_id("run"), budget=5)
    statements.clear()
    ctx.check_query_budget()
    ctx.check_query_budget()
    assert _count_query_execution(statements) == 2  # never skipped: the audit rows are counted instead


def test_model_budgets_read_counters_and_purpose_caps_hold(control_db, counters, statements, monkeypatch):
    from analystos.contracts.platform import LLMSettings, PlatformSettings
    from analystos.core.errors import BudgetExceeded
    from analystos.llm.router import CallContext
    from analystos.runtime.usage import DbUsageSink
    from analystos.services import platform_settings

    settings = PlatformSettings(llm=LLMSettings(purpose_run_caps={"summarization": {"calls": 2}}))
    monkeypatch.setattr(platform_settings, "get", lambda: settings)
    ws = _workspace({"workspace_monthly_cost_budget_usd": 1.0, "run_cost_budget_usd": 0.6})
    sink = DbUsageSink()
    ctx = CallContext(workspace_id=ws, run_id=new_id("run"))

    def call(purpose: str, cost: float) -> None:
        sink.record(ctx=ctx, purpose=purpose, profile="low_cost", provider="openrouter", model="m", status="ok", attempt=1,
                    latency_ms=1, input_tokens=100, output_tokens=10, cost_usd=cost, request_hash=None, error=None,
                    answered_by="llm_small", cost_source="provider")

    sink.check_budget(ctx, "summarization")  # seeds run, purpose and month counters
    statements.clear()
    call("summarization", 0.1)
    sink.check_budget(ctx, "summarization")
    call("summarization", 0.1)
    assert _sum_model_call(statements) == 0
    with pytest.raises(BudgetExceeded, match="per-run cap for summarization exhausted: calls 2 of 2"):
        sink.check_budget(ctx, "summarization")
    sink.check_budget(ctx, "planning")  # other purposes are not capped
    call("planning", 0.45)
    with pytest.raises(BudgetExceeded, match="run cost budget"):
        sink.check_budget(ctx, "planning")  # 0.65 >= 0.6, from the run counter
    assert _sum_model_call(statements) == 0
    other = CallContext(workspace_id=ws, run_id=new_id("run"))
    sink.check_budget(other, "planning")
    sink.record(ctx=other, purpose="planning", profile="p", provider="openrouter", model="m", status="ok", attempt=1, latency_ms=1,
                input_tokens=1, output_tokens=1, cost_usd=0.4, request_hash=None, error=None)
    with pytest.raises(BudgetExceeded, match="workspace monthly model budget"):
        sink.check_budget(other, "planning")  # 0.65 + 0.4 >= 1.0 this month, from the workspace counter
    assert _sum_model_call(statements) == 0
    assert sink.remaining_fraction(ctx) == 0.0


def test_token_savings_by_purpose_and_rung_and_missing_prices(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall

    tag = new_id("p")
    rows = [("ok", "llm_large", 1000, 0, 0.01, "provider"), ("cache_hit", "cache", 0, 1100, 0.0, "none"),
            ("skipped", "rules", 0, 2000, 0.0, "none"), ("skipped", "registry", 0, 900, 0.0, "none"),
            ("ok", "decision", 200, 0, 0.0, "missing_price")]
    with session_scope() as s:
        for status, rung, used, saved, cost, source in rows:
            s.add(ModelCall(purpose=tag, profile="-", provider="x", model="vendor/unpriced" if source == "missing_price" else "m",
                            status=status, attempt=1, input_tokens=used, output_tokens=0, cost_usd=cost, tokens_saved=saved,
                            answered_by=rung, cost_source=source))
    with TestClient(app) as api:
        token = api.post("/api/auth/login", json={"email": "admin@analystos.local", "password": "ChangeMe123!"}).json()["access_token"]
        body = api.get("/api/admin/token-savings", params={"days": 1}, headers={"Authorization": f"Bearer {token}"}).json()
    mine = body["by_purpose"][tag]
    assert set(mine["by_rung"]) == {"llm_large", "cache", "rules", "registry", "decision"}
    assert mine["by_rung"]["rules"]["tokens_saved"] == 2000 and mine["by_rung"]["registry"]["tokens_saved"] == 900
    assert mine["by_rung"]["llm_large"] == {"calls": 1, "answered": 1, "tokens_used": 1000, "tokens_saved": 0, "cost_usd": 0.01}
    assert mine["calls"] == 2 and mine["tokens_saved"] == 4000
    assert {"cache", "rules", "llm_large"} <= set(body["by_rung"])
    assert {"model": "vendor/unpriced", "calls": 1, "tokens": 200} in body["missing_price"]
    assert body["totals"]["missing_price_calls"] >= 1 and body["cost_complete"] is False and body["prices_version"]


def test_migration_0012_backfills_the_rung_and_reverts(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig12"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0011")
        with engine.begin() as c:
            for status, provider, profile in (("skipped", "deterministic", "-"), ("cache_hit", "openrouter", "coding"),
                                              ("ok", "typesafe", "decision"), ("ok", "openrouter", "low_cost"),
                                              ("error", "openrouter", "reasoning_strong"), ("refused", "openrouter", "coding")):
                c.execute(text("INSERT INTO model_call (purpose, profile, provider, model, status, attempt, latency_ms, input_tokens, "
                               "output_tokens, cost_usd, tokens_saved) VALUES ('p', :profile, :provider, 'm', :status, 1, 0, 0, 0, 0, 0)"),
                          {"status": status, "provider": provider, "profile": profile})
        command.upgrade(cfg, "0012")
        with engine.connect() as c:
            got = dict(c.execute(text("SELECT status, answered_by FROM model_call")).all())
        assert got == {"skipped": "rules", "cache_hit": "cache", "ok": got["ok"], "error": "llm_large", "refused": "rules"}
        with engine.connect() as c:
            assert sorted(r[0] for r in c.execute(text("SELECT answered_by FROM model_call WHERE status = 'ok'"))) == \
                ["decision", "llm_small"]
        cols = {c["name"]: c for c in inspect(engine).get_columns("model_call")}
        assert cols["answered_by"]["nullable"] is False and "cost_source" in cols
        command.downgrade(cfg, "0011")
        assert "answered_by" not in {c["name"] for c in inspect(engine).get_columns("model_call")}
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()
