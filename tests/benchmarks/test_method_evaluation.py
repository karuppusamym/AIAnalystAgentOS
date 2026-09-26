"""P4-03 method-specific evaluation in CI: per-method null behaviour, power near the materiality threshold,
selection effects, and the V01 suite broken down by method (thresholds in evaluation.method_checks)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluation import method_checks as mc  # noqa: E402
from evaluation.analytical import check as v01_check  # noqa: E402
from evaluation.analytical import run_component_suite  # noqa: E402


def test_null_power_and_selection_meet_their_thresholds():
    report = mc.run(reps=200)
    assert mc.check(report) == [], mc.check(report)
    by = {m.method: m for m in report.methods}
    assert set(by) == {"rate_by_segment", "numeric_by_segment", "correlation", "trend"}
    for m in by.values():
        # monotone in the effect, ~half at exactly the threshold (the estimate must clear it), full at 3x
        assert m.power["0x"] <= m.power["1x"] <= m.power["2x"] <= m.power["3x"]
        assert 0.2 <= m.power["1x"] <= 0.75 and m.power["3x"] >= 0.99
    sel = report.selection
    assert sel["same_data_fpr"] > 2 * sel["held_out_fpr"]  # testing on the selecting data is biased; held-out is not
    assert sel["null_quoted_rate_ratio_mean"] > 1.05  # the winner's curse: post-hoc top/bottom ratio > 1 under the null
    assert sel["planted_quoted_rate_ratio_mean_when_supported"] > sel["planted_true_rate_ratio"]


def test_v01_reports_every_tested_method_with_its_own_null_fdr():
    summary, _ = run_component_suite([1], null_seeds=[101], domains=("itsm",))
    assert summary.by_method and v01_check(summary) == []
    for name, row in summary.by_method.items():
        assert row["tested"] >= row["verified"] >= 0 and row["null_fdr"] <= mc.ALPHA, (name, row)
    assert sum(r["tested"] for r in summary.by_method.values()) == summary.tested
