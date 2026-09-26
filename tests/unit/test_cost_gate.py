"""P4-T10: the CI cost gate replays the recorded runs and fails a change that raises model calls or
tokens per run by more than 10%."""
import copy
import json

import pytest
from scripts import cost_gate

from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.llm.config import load_models_config


@pytest.fixture
def runs():
    return cost_gate.load_runs()


@pytest.fixture
def baseline():
    return json.loads(cost_gate.BASELINE.read_text())


def test_committed_runs_are_within_the_baseline(runs, baseline):
    assert {r["name"] for r in runs} == set(baseline["runs"]) and baseline["tolerance"] == pytest.approx(0.10)
    measured = cost_gate.measure(runs)
    assert cost_gate.compare(measured, baseline) == []
    assert measured["standard_run"]["calls"] <= 10  # spec v3 §4.2 target: ≤ 10 calls in a standard run


def test_a_deliberately_added_call_fails_the_gate(runs, baseline, tmp_path, capsys):
    """A new model call in the standard run (one more summarization call the rules could not avoid)."""
    standard = copy.deepcopy(next(r for r in runs if r["name"] == "standard_run"))
    standard["points"].append({"purpose": "summarization", "kind": "chat", "gate": "model", "tokens_hint": 900,
                               "request": None, "response": None})
    failures = cost_gate.compare(cost_gate.measure([standard]), baseline)
    assert any(f.startswith("standard_run: calls 8 > baseline 7") for f in failures), failures
    # The CLI exits non-zero on the same input (what fails the CI step).
    for run in [standard, *[r for r in runs if r["name"] != "standard_run"]]:
        (tmp_path / f"{run['name']}.json").write_text(json.dumps(run))
    (tmp_path / "baseline.json").write_text(json.dumps(baseline))
    assert cost_gate.main(["--fixtures", str(tmp_path)]) == 1
    assert "COST GATE FAILED: standard_run: calls" in capsys.readouterr().err


def test_turning_a_rule_purpose_model_first_fails_the_gate(runs, baseline, monkeypatch):
    """The regression P4-T02 removed: narratives back on the model by default (models.yaml ladder)."""
    cfg = load_models_config()
    monkeypatch.setitem(cfg.ladders, "insight_narrative", ["cache", "llm_small", "rules"])
    failures = cost_gate.compare(cost_gate.measure(runs), baseline)
    assert any(f.startswith("standard_run: calls") for f in failures) and any(f.startswith("novel_question: calls") for f in failures)


def test_disabling_the_response_cache_fails_the_gate(runs, baseline):
    settings = PlatformSettings(llm=LLMSettings(cache_enabled=False))
    measured = cost_gate.measure(runs, settings=settings)
    assert measured["novel_question"]["calls"] == baseline["runs"]["novel_question"]["calls"] + 1
    assert any(f.startswith("novel_question: calls") for f in cost_gate.compare(measured, baseline))


def test_within_tolerance_passes_and_fewer_calls_pass(baseline):
    base = baseline["runs"]["novel_question"]
    assert cost_gate.compare({"novel_question": {"calls": base["calls"], "tokens": int(base["tokens"] * 1.09)}}, baseline) == []
    assert cost_gate.compare({"novel_question": {"calls": 0, "tokens": 0}}, baseline) == []
    assert cost_gate.compare({"unknown_run": {"calls": 1, "tokens": 1}}, baseline) == [
        "unknown_run: no baseline (run with --update-baseline and commit it)"]


# ------------------------------------------------------------------ cheap first, escalate
def test_escalation_happens_only_on_a_failed_check(runs, baseline):
    """The escalation fixture: 4 model points, 2 answers fail their check (not JSON; empty hypotheses)."""
    run = next(r for r in runs if r["name"] == "escalation")
    got = cost_gate.replay_run(run)
    assert got["calls"] == 6 and [e["point"] for e in got["escalations"]] == [2, 3]
    assert got["by_model"]["anthropic/claude-sonnet-5"] == 2  # only the two escalations reach the large tier
    assert {e["reason"].split(":")[0] for e in got["escalations"]} == {"invalid_json", "validation"}
    # Every small answer valid: no call reaches the large tier.
    valid = copy.deepcopy(run)
    for p in valid["points"]:
        if p.get("escalation_response"):
            p["response"] = {**p["escalation_response"], "model": p["response"]["model"]}
            del p["escalation_response"]
    fine = cost_gate.replay_run(valid)
    assert fine["calls"] == 4 and fine["escalations"] == [] and "anthropic/claude-sonnet-5" not in fine["by_model"]
    assert cost_gate.compare({"escalation": got}, baseline) == []


def test_escalating_every_answer_or_large_first_routing_fails_the_gate(runs, baseline):
    """Escalation on success (an always-failing check) adds calls; Sonnet first (the routing that spent
    the owner's credit in a day) keeps the call count but fails on dollars."""
    run = copy.deepcopy(next(r for r in runs if r["name"] == "escalation"))
    for p in run["points"]:
        p["escalation_response"] = p.get("escalation_response") or p["response"]
        p["response"] = {**p["response"], "text": "not json"}
    failures = cost_gate.compare({"escalation": cost_gate.replay_run(run)}, baseline)
    assert any(f.startswith("escalation: calls") for f in failures), failures
    large_first = PlatformSettings(llm=LLMSettings(escalation={p: "always_large" for p in load_models_config().routing}))
    measured = cost_gate.measure(runs, settings=large_first)
    failures = cost_gate.compare(measured, baseline)
    assert any(f.startswith("novel_question: usd") for f in failures), failures
    assert measured["novel_question"]["calls"] == baseline["runs"]["novel_question"]["calls"]


def test_usd_report_compares_cheap_first_with_large_first(runs):
    report = cost_gate.usd_report(runs)
    novel = report["novel_question"]
    assert novel["cheap_first"]["usd"] * 4 < novel["large_first"]["usd"]
    std = report["standard_run"]
    assert std["cheap_first_model_first_ladders"]["usd"] * 4 < std["large_first_model_first_ladders"]["usd"]
