"""P4-V01 component tier in the default suite (no services): a small slice of the analytical
benchmark — ITSM, sales and finance with planted effects, and a global-null replicate each — must
meet the thresholds. CI runs the full component tier through scripts/benchmark_analytical.py --check,
and the platform tier (staging + gateway + a real run) in tests/integration/test_analytical_benchmark_platform.py."""
from __future__ import annotations

import pytest

from analystos.evaluation import datasets
from analystos.evaluation.analytical import check, classify, run_component_suite


def test_component_benchmark_meets_thresholds():
    summary, scores = run_component_suite([7], null_seeds=[107])
    assert not check(summary), (check(summary), summary.missed, summary.false_findings)
    assert summary.recall == 1.0 and summary.planted_total == 10
    assert all(s.false_positive == 0 for s in scores if not s.effects), summary.false_findings
    assert summary.null_tests > 50  # the FDR is measured on a real number of true nulls


@pytest.mark.parametrize("domain", datasets.DOMAINS)
def test_datasets_are_deterministic_and_truth_is_explicit(domain):
    a, b = datasets.build(domain, 3), datasets.build(domain, 3)
    assert a.frame.equals(b.frame)
    assert not a.frame.equals(datasets.build(domain, 4).frame)
    for p in a.planted:
        assert p.method == "pareto" or a.associated(p.outcome, p.segment)
    for col in a.null_columns:  # nulls are independent of every outcome
        assert not any(a.associated(col, p.outcome or p.segment) for p in a.planted)
    null = datasets.build(domain, 3, effects=False)
    assert not null.planted and list(null.frame.columns) == list(a.frame.columns)


def test_classification_uses_the_truth_graph():
    ds = datasets.build("sales", 1)
    spec = {"method": "numeric_by_segment", "outcome": {"type": "column", "column": "net_amount"},
            "segment": {"type": "column", "column": "customer_segment"}}
    assert classify(ds, spec, "Enterprise") == ("true", "sales_value_by_segment")
    assert classify(ds, spec, "Consumer") == ("true", None)  # a real effect, but not the planted top segment
    spec["segment"]["column"] = "payment_method"
    assert classify(ds, spec, "card") == ("false", None)
    assert classify(ds, {"method": "trend", "time": {"type": "date_trunc", "column": "order_date"}})[0] == "false"
    itsm = datasets.build("itsm", 1)
    after = {"method": "numeric_by_segment", "outcome": {"type": "duration_hours", "column": "opened_at", "end_column": "resolved_at"},
             "segment": {"type": "after_hours", "column": "opened_at"}}
    assert classify(itsm, after, True)[0] == "true"
    assert classify(datasets.build("itsm", 1, effects=False), after, True)[0] == "false"
