"""P4-V02 on the local stack: the Ask accuracy benchmark's `fake` and `off` tiers over the full ITSM,
sales and finance question sets. Data is uploaded, discovered and staged like a customer's; every
gold and answer statement goes through the gateway. CI runs this file as its own step
(`Ask benchmark`) with a small time budget; the live tier is `scripts/benchmark_ask.py --models live`."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


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


def test_off_tier_is_the_no_model_floor(control_db):
    """No provider key: the registry and the rules answer; no provider call is made, no restricted
    value is answered, and the registry's own phrasings are answered correctly."""
    from evaluation.ask import run

    r = run("off")
    o = r.summary["overall"]
    assert not r.problems, r.problems
    assert o["model"]["calls"] == 0 and o["restricted_leaks"] == 0
    assert set(o["answered_by"]) <= {"registry"}
    # A question the registry does not match is refused as no_model, never guessed.
    unmatched = [x for x in r.outcomes if x.expect == "answer" and not x.verified_query]
    assert unmatched and all(x.actual == "decline" and x.refusal_kind == "no_model" for x in unmatched)
    # Without a model, asking for a missing input only happens on a registry match.
    assert all(x.verified_query for x in r.outcomes if x.actual == "needs_input")
    # A near miss (another aggregate, an extra group or filter, another value than the one hard-coded)
    # is not served from the registry: no confident wrong answer, and the registry's own phrasings still answer.
    assert o["confident_wrong"] == 0, [x.id for x in r.outcomes if x.confident_wrong]
    assert o["by_tag"]["registry_exact"]["accuracy"] == 1.0 and o["by_tag"]["parameter"]["accuracy"] == 1.0
