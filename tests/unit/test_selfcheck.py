"""P7-04: the deterministic self-check library (skills/selfcheck.py). Each fixture is a mistake the
check must catch; the safe corrections must produce SQL that answers the same question."""
from __future__ import annotations

import sqlglot

from analystos.skills import selfcheck as sc
from analystos.skills.selfcheck import Observation


def _by(results, name):
    return next(r for r in results if r.check == name)


def test_a_clean_result_passes_every_check():
    obs = Observation(sql="SELECT priority, COUNT(*) AS n FROM sn.incident GROUP BY priority",
                      columns=["priority", "n"], rows=[["1", 10], ["2", 12]], row_count=2, history=[20.0, 23.0])
    results = sc.run(obs)
    assert [r.check for r in results] == ["empty_result", "magnitude", "truncation", "grouping", "fanout", "dq_gate"]
    assert sc.passed(results) and sc.safe_correction(results) is None


def test_empty_result_is_corrected_when_a_filter_value_differs_only_in_case():
    obs = Observation(sql="SELECT COUNT(*) AS n FROM sn.incident WHERE state = 'closed'", columns=["n"], rows=[],
                      row_count=0, column_values={"incident.state": ["Closed", "Open", "New"]})
    r = sc.empty_result(obs)
    assert not r.passed and r.correction is not None
    assert "'Closed'" in r.correction["sql"] and "'closed'" not in r.correction["sql"]
    sqlglot.parse_one(r.correction["sql"], read="postgres")  # still valid SQL


def test_empty_result_is_flagged_when_the_value_is_ambiguous_or_unknown():
    ambiguous = Observation(sql="SELECT * FROM t WHERE s = 'closed'", rows=[], row_count=0,
                            column_values={"s": ["Closed", "CLOSED"]})
    unknown = Observation(sql="SELECT * FROM t WHERE s = 'archived'", rows=[], row_count=0, column_values={"s": ["Closed"]})
    for obs in (ambiguous, unknown):
        r = sc.empty_result(obs)
        assert not r.passed and r.correction is None


def test_magnitude_against_history_and_the_unit_hint():
    ok = sc.magnitude(Observation(columns=["rate"], rows=[[0.31]], history=[0.3, 0.33, 0.29]))
    assert ok.passed
    jump = sc.magnitude(Observation(columns=["rate"], rows=[[31.0]], history=[0.3, 0.31, 0.29]))
    assert not jump.passed and "percent" in jump.detail
    flip = sc.magnitude(Observation(columns=["delta"], rows=[[-5.0]], history=[5.0, 6.0]))
    assert not flip.passed and "opposite sign" in flip.detail
    assert sc.magnitude(Observation(columns=["n"], rows=[[5]], history=[])).passed  # nothing to compare with


def test_truncation_reads_the_row_cap_and_the_population_record():
    assert not sc.truncation(Observation(rows=[[1]] * 3, row_count=3, truncated=True)).passed
    cut = {"check": "representative_population", "passed": False, "detail": "undeclared: 1,000 of 5,000 rows; truncated"}
    r = sc.truncation(Observation(rows=[[1]], row_count=1, populations={"sn.incident": cut}))
    assert not r.passed and "sn.incident" in r.detail
    fine = {"check": "representative_population", "passed": True, "detail": "not truncated"}
    assert sc.truncation(Observation(rows=[[1]], row_count=1, populations={"sn.incident": fine})).passed


def test_grouping_mistakes_are_flagged():
    dup = sc.grouping(Observation(columns=["priority", "n"], rows=[["1", 3], ["1", 4], ["2", 5]]))
    assert not dup.passed and "more than once" in dup.detail
    near = sc.grouping(Observation(columns=["team", "n"], rows=[["Network", 3], ["network ", 4]]))
    assert not near.passed and "case or spacing" in near.detail
    hidden = sc.grouping(Observation(sql="SELECT priority, COUNT(*) AS n FROM t GROUP BY priority, category",
                                     columns=["priority", "n"], rows=[["1", 3], ["2", 4]]))
    assert not hidden.passed and "category" in hidden.detail


RELS = [{"from_table": "sales.order_line", "from_column": "order_id", "to_table": "sales.orders", "to_column": "id",
         "cardinality": "many_to_one"}]


def test_fanout_is_flagged_for_a_sum_over_the_one_side():
    obs = Observation(sql="SELECT SUM(o.amount) FROM sales.orders o JOIN sales.order_line l ON l.order_id = o.id",
                      columns=["sum"], rows=[[100]], relationships=RELS)
    r = sc.fanout(obs)
    assert not r.passed and r.correction is None and "pre-aggregate" in r.detail


def test_fanout_count_of_the_one_side_key_is_safely_corrected():
    obs = Observation(sql="SELECT COUNT(o.id) AS orders FROM sales.orders o JOIN sales.order_line l ON l.order_id = o.id",
                      columns=["orders"], rows=[[40]], relationships=RELS)
    r = sc.fanout(obs)
    assert not r.passed and r.correction is not None
    assert "COUNT(DISTINCT o.id)" in r.correction["sql"]


def test_fanout_passes_for_the_many_side_measure_and_undeclared_joins():
    many = Observation(sql="SELECT SUM(l.qty) FROM sales.orders o JOIN sales.order_line l ON l.order_id = o.id",
                       rows=[[1]], relationships=RELS)
    assert sc.fanout(many).passed
    undeclared = Observation(sql="SELECT SUM(o.amount) FROM a o JOIN b l ON l.x = o.y", rows=[[1]], relationships=RELS)
    assert sc.fanout(undeclared).passed


def test_dq_gate_failures_flag_the_step():
    r = sc.dq_gate(Observation(dq=[{"asset": "rcp.clean_orders", "gate": "not_null(amount)", "status": "failed"}]))
    assert not r.passed and "clean_orders" in r.detail
    assert sc.dq_gate(Observation(dq=[{"asset": "x", "gate": "g", "status": "passed"}])).passed


def test_claim_numbers_must_bind_to_upstream_values():
    ok = sc.numbers(Observation(kind="claim", text="Breach rate is 31.2% across 1,204 incidents.",
                                upstream_values=[0.3121, 1204]))
    assert ok.passed
    bad = sc.numbers(Observation(kind="claim", text="Breach rate is 45%.", upstream_values=[0.3121]))
    assert not bad.passed and "45%" in bad.detail
    assert sc.run(Observation(kind="claim", text="No numbers here."))[0].passed


def test_a_recorded_finding_rebinds_to_its_rerun_method_result():
    """Live journey 2026-09-27: editing a method step re-ran the finding that reads it, and the finding's
    segment label ("= 1") and its rate ratio ("3.0x", in stat.highlights) did not bind, so an unchanged,
    verified claim came back flagged. Both are in the method's result and must bind."""
    from analystos.services.steps import numbers_of

    result = {"columns": ["segment", "n", "positives", "rate"],
              "rows": [["1", 6673, 640, 0.0959088866], ["3+", 3089, 900, 0.291356426]],
              "stat": {"n": 20000, "p_value": 0.0, "groups": [{"segment": "1", "rate": 0.0959}],
                       "highlights": {"top_rate": 0.2914, "rate_ratio": 3.038, "top_segment": "3+"}}}
    text = ("Records with reassignment count = 3+ have a missed SLA rate of 29.1% versus 9.6% for "
            "reassignment count = 1 (3.0x).")
    r = sc.numbers(Observation(kind="claim", text=text, upstream_values=numbers_of(result)))
    assert r.passed, r.detail
    wrong = sc.numbers(Observation(kind="claim", text=text.replace("3.0x", "4.0x"), upstream_values=numbers_of(result)))
    assert not wrong.passed and "4.0" in wrong.detail
