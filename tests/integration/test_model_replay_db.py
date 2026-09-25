"""P4-C09 against Postgres: model_call rows reference content-addressed payloads, and
`analystos replay-run` reconstructs and re-executes a run's model calls offline."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from tests.fakes import FakeTransport, chat_json

from analystos.contracts.platform import PlatformSettings
from analystos.core.errors import UpstreamUnavailable
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import ModelCall, ModelPayload
from analystos.llm.cache import ResponseCache
from analystos.llm.jev import JevDecisions
from analystos.llm.replay import load_run_calls, run_report
from analystos.llm.router import CallContext, ModelRouter
from analystos.runtime.usage import DbUsageSink

pytestmark = pytest.mark.integration


def test_run_model_calls_are_stored_and_replay_offline(control_db, capsys, tmp_path):
    run_id = new_id("run")
    attempts = []

    def chat(p):
        attempts.append(p["model"])
        return UpstreamUnavailable("503") if len(attempts) == 1 else chat_json({"questions": ["q"]}, model=p["model"])

    answers = {"priority_0": {"score": 2, "probabilities": {"low": 0.1, "medium": 0.2, "high": 0.7}, "confidence": 0.8}}
    platform = PlatformSettings()
    router = ModelRouter(transport=FakeTransport(chat=chat, decide=lambda p: {"answers": answers, "usage": {}}),
                         sink=DbUsageSink(), api_key_lookup=lambda _e: "sk-test-000000000000000000000000", max_retries=0,
                         settings_provider=lambda: platform, cache=ResponseCache(None))
    ctx = CallContext(run_id=run_id, prompt_version="planning.v1@0123456789ab")
    router.complete_json("planning", "sys", '{"objective":"resolution time"}', ctx=ctx)
    verdicts = JevDecisions(router).score_hypotheses("resolution time", {"0": "Priority drives resolution"}, ctx=ctx)
    assert verdicts["0"].probabilities["high"] == 0.7

    with session_scope() as s:
        rows = list(s.scalars(select(ModelCall).where(ModelCall.run_id == run_id).order_by(ModelCall.id)))
        assert [r.status for r in rows] == ["error", "ok", "ok"]
        assert rows[0].request_ref == rows[1].request_ref  # the retried request is stored once
        assert rows[0].response_ref is None and rows[1].response_ref and rows[2].response_ref
        refs = {r.request_ref for r in rows} | {r.response_ref for r in rows if r.response_ref}
        assert s.scalar(select(func.count()).select_from(ModelPayload).where(ModelPayload.hash.in_(refs))) == len(refs)

    calls = load_run_calls(run_id)
    assert calls[1].response["text"] == json.dumps({"questions": ["q"]})
    assert calls[2].to_dict()["decision"]["priority_0"]["probabilities"] == {"low": 0.1, "medium": 0.2, "high": 0.7}

    report = run_report(run_id, check=True)
    assert report["summary"]["decisions"] == 1 and report["replay"]["matched"] == report["replay"]["checked"] == 2

    from analystos.cli import main

    out = tmp_path / "replay.json"
    assert main(["replay-run", run_id, "--check", "--out", str(out)]) == 0
    saved = json.loads(out.read_text())
    assert saved["run_id"] == run_id and len(saved["calls"]) == 3
    assert "2/2 calls reproduced offline" in capsys.readouterr().err


def test_migration_0006_applies_and_reverts(control_db):
    """The hand-written migration matches the model (fresh database, upgrade to head, downgrade one)."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig"
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
        command.upgrade(cfg, "0006")
        insp = inspect(engine)
        assert {"request_ref", "response_ref"} <= {c["name"] for c in insp.get_columns("model_call")}
        assert "model_payload" in insp.get_table_names()
        command.downgrade(cfg, "0005")
        assert "model_payload" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        with admin.connect() as c:  # alembic's env keeps its own pooled connections open
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()
