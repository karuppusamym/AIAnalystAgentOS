"""Where P8-15 (held-out confirmation) meets P8-16 (joined segments) and older runs; each case was found
on the live retail re-run (run_cf4ead61e934) or a concurrent benchmark run bound before the upgrade."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.contracts.analysis import AnalysisSpec, Partition
from analystos.core.errors import OutputContractViolation

JOINED = AnalysisSpec.model_validate({
    "method": "rate_by_segment", "asset": "shop.orders",
    "joins": [{"from_column": "customer_id", "asset": "shop.customers", "to_column": "customer_id"}],
    "outcome": {"type": "later_than", "column": "promised_date", "end_column": "delivered_date", "label": "late"},
    "segment": {"type": "column", "column": "region", "via": "customer_id", "label": "customer region"}})


@pytest.mark.parametrize("dialect", ["postgres", "duckdb"])
def test_the_held_out_contrast_of_a_joined_segment_filters_its_own_table(dialect):
    """The gateway rejected `WHERE "t"."region" IN (...)`: region lives on the joined customers table."""
    from analystos.evidence.holdout import claim_spec
    from analystos.skills import sqlbuild

    spec, contrast = claim_spec(JOINED, {"top": "West", "baseline": "South", "groups": 4})
    assert contrast == ["West", "South"] and spec.filters[-1].via == "customer_id"
    held = Partition(salt="s1", key=["order_id"], fraction=0.3, side="holdout", claim_locked_at="2026-09-29T00:00:00+00:00")
    sql = str(sqlbuild.compile_spec(spec, dialect, partition=held))
    assert '"j_customer_id"."region" IN' in sql and '"t"."region"' not in sql


def test_the_default_rate_metric_reads_boolean_and_one_zero_flags():
    """`CASE WHEN "flag" THEN` failed in Postgres on the 1/0 flags joined specs derive."""
    import duckdb

    from analystos.agents.semantic import _rate_expr

    expr = _rate_expr("late")
    con = duckdb.connect()
    for values in ("(1),(0),(1),(NULL)", "(TRUE),(FALSE),(TRUE),(NULL)"):
        got = con.execute(f"SELECT {expr} FROM (VALUES {values}) AS t(late)").fetchone()[0]
        assert got == pytest.approx(0.5)  # 2 of 4 records; a missing value counts as not holding


def _ctx(allows: bool):
    def check_output(type_, content):
        if content.get("role") == "holdout" and not allows:
            raise OutputContractViolation("role: 'holdout' is not one of ['primary', 'verification']")
    return SimpleNamespace(check_output=check_output)


def test_a_run_bound_to_the_old_critic_skips_the_held_out_step_instead_of_failing():
    from analystos.agents import critic

    assert critic._bound_allows_holdout(_ctx(True)) is True
    assert critic._bound_allows_holdout(_ctx(False)) is False
    record = critic.confirm_on_holdout(_ctx(False), {"spec": JOINED, "spec_d": JOINED.model_dump(), "code": "I-1", "alpha": 0.05,
                                                     "stat_d": {"details": {"partition": {}}}, "hypothesis_id": "h1",
                                                     "discovery": Partition(key=["order_id"], fraction=0.3)}, "West", "higher")
    assert record.evaluated is False and not record.supported and "critic version" in record.reason
