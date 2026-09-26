"""P4-V02 on the local stack: the Ask accuracy benchmark's `fake` and `off` tiers over the full ITSM,
sales and finance question sets. Data is uploaded, discovered and staged like a customer's; every
gold and answer statement goes through the gateway. CI runs this file as its own step
(`Ask benchmark`) with a small time budget; the live tier is `scripts/benchmark_ask.py --models live`.

Both tiers are also evaluation gates (P7-07): their metrics must hold the owner's thresholds in
config/eval_gates.yaml (`ask_fake`, `ask_off`). With ANALYSTOS_EVAL_RESULTS_DIR set (CI) the gate
result of each tier is written there and attached to the build."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _gate(tier: str, run) -> None:
    from evaluation import gates as G

    gates = G.load()
    metrics = G.ask_metrics(run)
    failures = G.check(tier, metrics, gates)
    out = os.environ.get("ANALYSTOS_EVAL_RESULTS_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{tier}.json").write_text(json.dumps({
            "config": {"version": gates.version, "sha256": gates.digest}, "tier": tier, "metrics": metrics,
            "failures": failures, "status": "failed" if failures else "passed"}, indent=1, default=str) + "\n")
    assert not failures, failures


def test_fake_tier_harness_gateway_refusals_and_scoring(control_db):
    """The oracle writes the gold SQL, so every model-written answer must match; every careless
    probe (another table, a write, a restricted column) must be refused by the gateway."""
    from evaluation.ask import run

    r = run("fake")
    o = r.summary["overall"]
    assert not r.problems, r.problems
    assert o["questions"] >= 120 and o["restricted_leaks"] == 0 and o["errors"] == 0
    assert o["answered_by"].get("model", 0) > 0 and o["model"]["calls"] > 0
    for reason in ("out_of_scope", "write", "restricted"):
        d = o["decline_reasons"][reason]
        assert d["governed"] == d["n"], (reason, d)
    # Everything the registry did not answer reached the oracle and matched its gold result.
    by_model = [x for x in r.outcomes if x.answered_by == "model"]
    assert by_model and all(x.match for x in by_model)
    _gate("ask_fake", r)


def test_off_tier_is_the_no_model_floor(control_db):
    """No provider key: the registry and the rules answer; no provider call is made, no restricted
    value is answered, and the registry's own phrasings are answered correctly."""
    from evaluation.ask import run

    r = run("off")
    o = r.summary["overall"]
    assert not r.problems, r.problems
    assert o["model"]["calls"] == 0 and o["restricted_leaks"] == 0
    assert set(o["answered_by"]) <= {"registry", "rules"} and o["answered_by"].get("rules", 0) > 0
    # A question the registry does not match is answered by the catalog rules (correctly), asked back
    # when a phrase names several columns, or refused naming the cause (no API key), never guessed.
    unmatched = [x for x in r.outcomes if x.expect == "answer" and not x.verified_query]
    assert unmatched and all((x.actual == "answer" and x.answered_by == "rules" and x.match)
                             or (x.actual == "clarify" and x.answered_by is None)
                             or (x.actual == "decline" and x.refusal_kind == "no_api_key") for x in unmatched), \
        [(x.id, x.actual, x.refusal_kind, x.match_note) for x in unmatched]
    # Without a model, asking for a missing input only happens on a registry match.
    assert all(x.verified_query for x in r.outcomes if x.actual == "needs_input")
    # The rules never answer a question the labels say must not be answered.
    assert not [x.id for x in r.outcomes if x.answered_by == "rules" and x.expect != "answer"]
    # A near miss (another aggregate, an extra group or filter, another value than the one hard-coded)
    # is not served from the registry: no confident wrong answer, and the registry's own phrasings still answer.
    assert o["confident_wrong"] == 0, [x.id for x in r.outcomes if x.confident_wrong]
    assert o["by_tag"]["registry_exact"]["accuracy"] == 1.0 and o["by_tag"]["parameter"]["accuracy"] == 1.0
    _gate("ask_off", r)


def test_governed_slice_compiles_identical_sql_and_never_mislabels(control_db):
    """P7-02: each governed question is answered from the approved model version (labelled governed), its
    SQL is exactly the compiler's for the labelled query, its rows match the gold SQL, and asking again
    gives the same SQL; questions the model cannot express are never labelled governed. No model call."""
    from evaluation.ask_governed import run_governed

    r = run_governed("off")
    s = r.summary
    failed = [(o.id, o.governance, o.sql_equivalent, o.match, o.match_note, o.deterministic, o.error)
              for o in r.outcomes if not o.passed]
    assert not failed, failed
    assert s["governed_items"] >= 15 and s["errors"] == 0 and s["false_governed"] == 0
    assert s["governed_rate"] == s["sql_equivalence"] == s["execution_accuracy"] == s["deterministic"] == 1.0
    assert s["model_calls"] == 0
