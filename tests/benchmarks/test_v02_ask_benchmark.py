"""P4-V02 in the default suite (no services): the labelled question sets are well formed, execution
match compares results the way the report says, and the metrics count refusals per class. The tiers
themselves run against the stack (tests/integration/test_ask_benchmark.py, scripts/benchmark_ask.py)."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from evaluation import ask
from evaluation.ask import OracleTransport, Outcome, Question, metrics, results_match


@pytest.mark.parametrize("domain", ask.DOMAINS)
def test_question_sets_are_well_formed(domain):
    qs = ask.load_set(domain)
    assert ask.problems(qs) == []
    assert len(qs.questions) >= 40
    kinds = {q.expect for q in qs.questions}
    assert kinds == set(ask.OUTCOMES)
    assert {q.reason for q in qs.questions if q.expect == "decline"} == set(ask.DECLINE_REASONS)
    assert sum("paraphrase" in q.tags for q in qs.questions) >= 5 and sum("novel" in q.tags for q in qs.questions) >= 10
    # Every enum parameter of the registry names a real column of the dataset.
    _, frame, _ = ask.frame_for(domain, 1)
    for entry in qs.registry:
        for p in entry.get("parameters") or []:
            assert p["column"] in frame.columns and p.get("values_from", p["column"]) in frame.columns
    assert qs.restricted_column in frame.columns and frame[qs.restricted_column].notna().all()


def test_restricted_column_does_not_change_the_v01_data():
    from evaluation.datasets import build

    ds, frame, qs = ask.frame_for("sales", 1)
    assert frame.drop(columns=[qs.restricted_column]).equals(build("sales", 1).frame)
    assert frame[qs.restricted_column].equals(ask.frame_for("sales", 1)[1][qs.restricted_column])


def test_problems_catch_a_bad_set():
    qs = ask.load_set("itsm")
    qs.questions = qs.questions[:3] + [Question(id="x", domain="itsm", q="a", expect="answer"),
                                       Question(id="y", domain="itsm", q="b", expect="decline", reason="restricted", probe_sql="SELECT 1")]
    found = " ".join(ask.problems(qs))
    assert "< 40" in found and "gold SQL is required" in found and "must read caller_email" in found


def test_results_match_is_order_insensitive_with_tolerance_and_extra_columns():
    gold = [["Network", Decimal("17.123456")], ["Database", 9.5]]
    assert results_match(gold, [["Database", 9.50000001], ["Network", 17.1234561]]) == (True, "")
    # columns in another order, and an extra answer column
    assert results_match(gold, [[9.5, "Database", 3], [17.123456, "Network", 4]])[0]
    # a value off beyond tolerance, a missing row, a percent for a fraction
    assert not results_match(gold, [["Network", 17.2], ["Database", 9.5]])[0]
    assert results_match(gold, [["Network", 17.123456]])[1].startswith("row count")
    assert not results_match([[0.15]], [[15.0]])[0]
    # the right values, wrongly paired
    assert results_match([["a", 1], ["b", 2]], [["a", 2], ["b", 1]]) == (False, "same columns, different rows")


def test_results_match_normalises_types():
    ts = datetime(2025, 3, 1, tzinfo=UTC)
    assert results_match([[ts, 3]], [["2025-03-01T00:00:00+00:00", "3"]])[0]
    assert results_match([[date(2025, 3, 1)]], [["2025-03-01 00:00:00"]])[0]
    assert results_match([[True]], [[1]])[0] and results_match([[None, "x"]], [[None, " x "]])[0]
    assert results_match([], [])[0]


def _o(expect, actual, *, match=None, kind=None, reason=None, tags=("novel",), calls=0, ms=10):
    return Outcome(id=f"{expect}-{actual}-{kind}", domain="d", question="q", expect=expect, reason=reason, tags=list(tags),
                   actual=actual, refusal_kind=kind, match=match, model_calls=calls, latency_ms=ms,
                   answered_by="model" if actual == "answer" else None)


def test_metrics_count_accuracy_confident_wrong_and_refusals():
    outcomes = [
        _o("answer", "answer", match=True, calls=1),
        _o("answer", "answer", match=False, calls=2),  # confident wrong
        _o("answer", "decline", kind="no_model"),
        _o("needs_input", "needs_input", kind="needs_input"),
        _o("clarify", "decline", kind="no_model"),
        _o("decline", "decline", kind="sql_rejected", reason="restricted"),
        _o("decline", "decline", kind="no_model", reason="out_of_scope"),
        _o("decline", "answer", reason="write"),  # confident wrong
    ]
    m = metrics(outcomes)
    assert m["execution_accuracy"] == round(1 / 3, 4) and m["answer_items_answered"] == 2
    assert m["confident_wrong"] == 2 and m["answered_precision"] == round(1 / 3, 4)
    assert m["refusal"]["decline"] == {"expected": 3, "predicted": 4, "correct": 2, "precision": 0.5, "recall": round(2 / 3, 4)}
    assert m["refusal"]["needs_input"]["precision"] == 1.0 and m["refusal"]["clarify"]["recall"] == 0.0
    assert m["decline_reasons"]["restricted"]["governed"] == 1 and m["decline_reasons"]["out_of_scope"]["governed"] == 0
    assert m["model"]["calls"] == 3 and m["latency_ms"]["p50"] == 10


def test_check_flags_leaks_errors_and_off_tier_model_calls():
    leak = _o("decline", "answer", reason="restricted", calls=1)
    leak.restricted_leak = True
    broken = _o("answer", "error")
    broken.error = "boom"
    outs = [leak, broken]
    found = " ".join(ask.check("off", ask.summarize(outs), outs))
    assert "restricted answers" in found and "boom" in found and "provider calls" in found


def test_stratified_limit_covers_every_outcome_class():
    picked = ask._stratified(ask.load_set("finance").questions, 8)
    assert len(picked) == 8 and {q.expect for q in picked} == set(ask.OUTCOMES)


def test_oracle_answers_the_longest_question_in_the_prompt():
    oracle = OracleTransport()
    oracle.answers = {"Monthly revenue": "SELECT 1", "Monthly revenue by channel": "SELECT 2", "Is it?": None}
    prompt = {"messages": [{"role": "user", "content": json.dumps({"question": "Monthly revenue by channel"})}]}
    out = json.loads(oracle.chat(base_url="", api_key="", payload=prompt, timeout=1)["choices"][0]["message"]["content"])
    assert out["sql"] == "SELECT 2"
    none = oracle.chat(base_url="", api_key="", payload={"messages": [{"content": "Is it?"}]}, timeout=1)
    assert json.loads(none["choices"][0]["message"]["content"]) == {}
