"""The sales pack benchmark through the full platform: the seeded retail database as a SQLite source,
crawled, staged and analysed by the local orchestrator with no model provider. The sales pack must
auto-enable from the catalog, every planted effect must end as a verified finding naming the planted
segment, and the null-control column (payment method) must yield no verified finding."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration

PACK = Path(__file__).resolve().parents[2] / "packs" / "sales"


def _generator():
    spec = importlib.util.spec_from_file_location("sales_benchmark_generator", PACK / "benchmark" / "generator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wait(run_id: str, statuses: set[str], timeout: float = 900) -> str:
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


def test_sales_benchmark_end_to_end_without_models(control_db, monkeypatch):
    import yaml

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.runtime.context import default_router

    get_settings.cache_clear()
    default_router.cache_clear()
    upload = Path(get_settings().upload_dir)
    bench = yaml.safe_load((PACK / "benchmark" / "benchmark.yaml").read_text())
    try:
        from analystos.db.base import session_scope
        from analystos.db.models import AgentMessage, Hypothesis, Insight, User
        from analystos.services.runs import create_run
        from analystos.services.sources import discover_source, register_source, select_assets
        from analystos.services.workspaces import create_workspace

        with session_scope() as s:
            admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
            ws = create_workspace(s, admin, name="sales pack benchmark",
                                  objective="Find what drives returns, order value and shipping time in retail orders")
            s.flush()
            (upload / ws.id).mkdir(parents=True, exist_ok=True)
            _generator().build_shop_db(upload / ws.id / f"shop-{new_id('t')}.db")
            fname = next((upload / ws.id).glob("shop-*.db")).name
            src = register_source(s, admin, ws.id, kind="sqlite", name="Retail shop", config={"path": f"{ws.id}/{fname}"},
                                  secret_ref=None)
            s.flush()
            ws_id, src_id = ws.id, src.id
            s.expunge(admin)
        discover_source(admin, src_id)
        select_assets(admin, src_id, ["orders"])
        run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
        assert _wait(run.id, {"COMPLETED", "FAILED"}) == "COMPLETED"

        with session_scope() as s:
            rows = s.execute(select(Hypothesis.spec, Insight.title).join(Insight, Insight.hypothesis_id == Hypothesis.id)
                             .where(Insight.run_id == run.id, Insight.status == "verified")).all()
            decisions = [m.data or {} for m in s.scalars(select(AgentMessage).where(AgentMessage.run_id == run.id))]
        assert any("pack.sales@1.0.0" in (d.get("domain_packs") or []) for d in decisions), "sales pack not auto-enabled"
        col = lambda d: (d or {}).get("column")  # noqa: E731
        for planted in bench["planted"]:
            m = planted["match"]
            hits = [t for spec, t in rows if spec["method"] == m["method"] and col(spec.get("outcome")) == m["outcome"]
                    and col(spec.get("segment")) == m["segment"]]
            assert any(planted["expect_top"].lower() in t.lower() for t in hits), (planted["id"], [t for _, t in rows])
        nulls = {c for n in bench["null_controls"] for c in n["columns"]}
        assert not [t for spec, t in rows if col(spec.get("segment")) in nulls], [t for _, t in rows]
    finally:
        get_settings.cache_clear()
        default_router.cache_clear()
