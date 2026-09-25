"""P4-V01 platform tier: the ITSM, sales and finance benchmark datasets uploaded as sources, staged by
the loader and analysed by real runs (local orchestrator, no model provider: the rule path), every
query through the gateway. Verified findings are scored against the datasets' truth graphs and must
meet the benchmark thresholds; a global-null dataset must yield no false verified finding."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def no_models(control_db, monkeypatch):
    from analystos.core.config import get_settings
    from analystos.runtime.context import default_router

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    get_settings.cache_clear()
    default_router.cache_clear()
    yield
    get_settings.cache_clear()
    default_router.cache_clear()


@pytest.mark.parametrize("domain", ["itsm", "sales", "finance"])
def test_platform_benchmark_meets_thresholds(no_models, domain):
    from analystos.evaluation.analytical import check, run_platform, summarize

    scores = [run_platform(domain, 1, effects=True), run_platform(domain, 101, effects=False)]
    summary = summarize(scores, "platform")
    detail = [(s.effects, s.status, [(f.truth, f.planted, f.method, f.outcome, f.segment, f.statement) for f in s.findings])
              for s in scores]
    assert not check(summary), (check(summary), detail)
    assert summary.recall == 1.0, (summary.missed, detail)
    assert scores[1].false_positive == 0, detail
