"""Domain-pack benchmarks (P4-X07, spec v3 §3.6): every installed pack's dataset with planted effects
goes through the deterministic pipeline (templates -> validation -> statistics -> BH -> independent
verification) with no model. Every planted effect must be found, with the planted top segment, and
every null control rejected. Runs in the default (no services) suite, so CI runs it on every push."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.capabilities import packs  # noqa: E402
from pack_benchmark import run_benchmark  # noqa: E402

BENCHMARKED = [p for p in packs.installed() if p.benchmark]


def test_both_domains_ship_a_benchmark():
    assert {"pack.itsm", "pack.sales"} <= {p.id for p in BENCHMARKED}
    for p in BENCHMARKED:
        assert p.benchmark.get("planted") and p.benchmark.get("null_controls"), p.id


@pytest.mark.parametrize("pack", BENCHMARKED, ids=[p.name for p in BENCHMARKED])
def test_planted_effects_found_and_null_controls_rejected(pack):
    result = run_benchmark(pack)
    assert not result.rejected_by_validation, result.rejected_by_validation  # the pack's templates fit its own data
    summary = [(t.proposal.get("statement"), t.found, t.top) for t in result.tested]
    assert result.planted and all(result.planted.values()), (result.planted, summary)
    assert result.nulls_rejected and all(result.nulls_rejected.values()), (result.nulls_rejected, summary)
    templated = [t for t in result.tested if t.proposal.get("pack") == pack.id]
    assert templated, "the pack's own templates proposed nothing"
