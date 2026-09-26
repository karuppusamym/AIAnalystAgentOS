"""P4-03: typed fact bindings, evidence dimensions, discovery vs confirmation, data-version manifest and
legacy badge migration. No services: pure functions over statistic payloads and manifest records.

The adversarial cases are the ones the design review names: a real number attached to the wrong group,
the wrong unit (hours vs days, percent vs fraction vs ratio) or the reversed direction must not bind."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from analystos import methods
from analystos.agents.insight import template_text
from analystos.contracts.evidence import Binding, Confirmation, EvidenceBundle, Validation
from analystos.evidence import bundle as B
from analystos.evidence import confirmation as C
from analystos.evidence import manifest as M
from analystos.evidence.facts import bind_finding, facts_from, outcome_unit
from analystos.staging.loader import ContentFingerprint

ROOT = Path(__file__).resolve().parents[2]

RATE_SPEC = {"method": "rate_by_segment", "asset": "src_x.incident",
             "outcome": {"type": "equals", "column": "made_sla", "value": False, "label": "missed SLA"},
             "segment": {"type": "column", "column": "priority", "label": "priority"}}
RATE_STAT = {"test": "chi_square_independence", "n": 20000, "p_value": 1e-14, "p_adjusted": 1e-12, "effect_size": 0.21,
             "effect_label": "cramers_v",
             "highlights": {"top_segment": "P1", "top_rate": 0.412, "top_n": 2000, "baseline_segment": "P4",
                            "baseline_rate": 0.131, "baseline_n": 9000, "rate_ratio": 3.14, "n_groups": 4},
             "groups": [{"segment": s, "n": n} for s, n in (("P1", 2000), ("P2", 4000), ("P3", 5000), ("P4", 9000))]}
DURATION_SPEC = {"method": "numeric_by_segment", "asset": "src_x.incident",
                 "outcome": {"type": "duration_hours", "column": "opened_at", "end_column": "resolved_at",
                             "label": "resolution time"},
                 "segment": {"type": "column", "column": "assignment_group", "label": "group"}}
DURATION_STAT = {"test": "kruskal_wallis_h", "n": 8000, "p_value": 1e-9, "p_adjusted": 1e-8, "effect_size": 0.04,
                 "effect_label": "epsilon_squared",
                 "highlights": {"top_segment": "Network", "top_median": 12.0, "baseline_segment": "Desk",
                                "baseline_median": 4.5, "median_ratio": 2.667, "n_groups": 3},
                 "groups": [{"segment": "Network", "n": 1500}, {"segment": "Desk", "n": 4000}, {"segment": "Apps", "n": 2500}]}
TREND_SPEC = {"method": "trend", "asset": "src_x.incident", "time": {"type": "date_trunc", "column": "opened_at", "grain": "week"}}
TREND_STAT = {"test": "ols_linear_trend", "n": 58, "p_value": 7e-5, "p_adjusted": 2e-4, "effect_size": -0.4416,
              "effect_label": "pct_change_fitted",
              "highlights": {"direction": "decreasing", "pct_change": -0.4416, "first_period": "2025-09-01",
                             "last_period": "2027-01-04", "n_periods": 58}}


def bound(spec, stat, *texts) -> Binding:
    return bind_finding(spec, stat, facts_from(spec, stat), *texts)


# ------------------------------------------------------------------------------------ typed facts
def test_facts_are_typed_with_unit_group_and_direction():
    facts = {f.role: f for f in facts_from(RATE_SPEC, RATE_STAT, query_ids=("q1",), result_hashes=("h1",))}
    assert (facts["top_rate"].unit, facts["top_rate"].subject, facts["top_rate"].metric) == ("fraction", "P1", "missed SLA")
    rr = facts["rate_ratio"]
    assert (rr.kind, rr.unit, rr.subject, rr.baseline, rr.direction, rr.primary) == ("comparison", "ratio", "P1", "P4", "higher", True)
    assert rr.query_ids == ["q1"] and rr.result_hashes == ["h1"] and rr.method == "chi_square_independence"
    assert facts["p_adjusted"].unit == "probability" and facts["n"].unit == "count"
    assert {f.subject for f in facts_from(RATE_SPEC, RATE_STAT) if f.role == "group_n"} == {"P2", "P3"}
    assert outcome_unit(DURATION_SPEC) == "hours"
    assert outcome_unit({"outcome": {"type": "column", "column": "shipping_days"}}) == "days"
    trend = {f.role: f for f in facts_from(TREND_SPEC, TREND_STAT)}
    assert (trend["pct_change"].kind, trend["pct_change"].direction, trend["pct_change"].unit) == ("change", "decrease", "fraction")


def test_every_method_template_binds_to_its_own_facts():
    for spec, stat in ((RATE_SPEC, RATE_STAT), (DURATION_SPEC, DURATION_STAT), (TREND_SPEC, TREND_STAT)):
        title, text = template_text(stat, spec)
        b = bind_finding(spec, stat, methods.get(spec["method"]).facts(spec, stat), title, text)
        assert b.ok, (text, b.problems)
        assert all(m.fact_id for m in b.mentions)


def test_trend_template_quotes_the_fraction_as_a_percent_with_its_direction():
    """Regression found by the typed binding: the old template printed pct_change -0.4416 as "-0.4%"."""
    _, text = template_text(TREND_STAT, TREND_SPEC)
    assert "fell by 44.2%" in text and "Weekly" in text
    assert not bound(TREND_SPEC, TREND_STAT, "Weekly volume changed by -0.4% from first to last period.").ok


# ------------------------------------------------------------------------------------ adversarial swaps
GOOD_RATE = "Priority P1 misses SLA 41.2% of the time versus 13.1% for P4 (3.1x)."


@pytest.mark.parametrize("text", [
    GOOD_RATE,
    "Missed SLA rate is 41.2% for P1 and 13.1% for P4.",
    "P1 is 3.1x higher than P4.",
    "P4 has a lower missed SLA rate than P1.",
    "Of 20,000 incidents, P1 (n=2,000) misses SLA 41.2% of the time.",
])
def test_correct_narratives_bind(text):
    b = bound(RATE_SPEC, RATE_STAT, text)
    assert b.ok, b.problems


@pytest.mark.parametrize("text,problem", [
    # swapped group: both numbers are real, attached to the wrong priority
    ("Priority P4 misses SLA 41.2% of the time versus 13.1% for P1 (3.1x).", "belongs to P1, not P4"),
    ("Missed SLA rate is 41.2% for P4 and 13.1% for P1.", "belongs to P1, not P4"),
    # swapped group in a direction claim without a number
    ("Missed SLA concentrates in priority = P4.", "says P4 is higher"),
    ("Missed SLA concentrates in P2.", "names P2 as highest"),
    # reversed direction
    ("P1 is 3.1x lower than P4.", "says P1 is lower"),
    ("P4 is 3.1x higher than P1.", "says P4 is higher"),
    # percent vs fraction vs ratio
    ("P1 misses SLA at a rate of 41.2 versus 13.1 for P4.", "no computed fact"),
    ("P1 misses SLA 0.412% of the time.", "no computed fact"),
    ("P1 has a 0.4x rate.", "no computed fact"),
    ("P1 misses SLA 314% more often than P4.", "no computed fact"),
    # invented number (the legacy guard also let 1, 2 and 3 through unconditionally)
    ("P1 misses SLA 57% of the time.", "no computed fact"),
    ("P1 misses SLA 2% more often.", "no computed fact"),
])
def test_swapped_group_unit_and_direction_do_not_bind(text, problem):
    b = bound(RATE_SPEC, RATE_STAT, text)
    assert not b.ok and any(problem in p for p in b.problems), b.problems


def test_hours_versus_days_swap_does_not_bind():
    assert bound(DURATION_SPEC, DURATION_STAT, "Median resolution time is 12.0 hours for Network versus 4.5 hours for Desk.").ok
    assert bound(DURATION_SPEC, DURATION_STAT, "Network takes a median 0.5 days to resolve.").ok  # 12 h = 0.5 d
    swapped = bound(DURATION_SPEC, DURATION_STAT, "Median resolution time is 12.0 days for Network versus 4.5 days for Desk.")
    assert not swapped.ok and len(swapped.problems) == 2
    group = bound(DURATION_SPEC, DURATION_STAT, "Median resolution time is 4.5 hours for Network.")
    assert not group.ok and "belongs to Desk, not Network" in group.problems[0]


def test_reversed_change_direction_does_not_bind():
    assert bound(TREND_SPEC, TREND_STAT, "Weekly volume fell by 44.2% over the period.").ok
    assert bound(TREND_SPEC, TREND_STAT, "Weekly volume changed by -44.2%.").ok
    for text, problem in (("Weekly volume rose by 44.2% over the period.", "says otherwise"),
                          ("Weekly volume changed by 44.2%.", "direction stated"),
                          ("Weekly volume changed by +44.2%.", "no computed fact"),
                          ("Incident volume is increasing week over week.", "wrong direction")):
        b = bound(TREND_SPEC, TREND_STAT, text)
        assert not b.ok and any(problem in p for p in b.problems), (text, b.problems)


# ------------------------------------------------------------------------------------ evidence bundle
def _entry(**kw):
    snap = {"rows_staged": 20000, "content_fingerprint": "c" * 64, "sampling_method": "full", "load_id": "load_1",
            "staged_at": "2026-09-26T10:00:00"}
    snap.update(kw)
    return M.entry_from("src_x.incident", "s1", "staged", snapshot=snap)


REV_CHECKS = [{"check": c, "passed": True, "detail": ""} for c in
              ("method_fit", "sample_size", "significance_after_bh", "effect_size", "representative_population",
               "no_overreach", "fact_binding", "data_version_stable", "reproducible_rerun", "second_method")]


def _assemble(**over):
    facts = facts_from(RATE_SPEC, RATE_STAT)
    kw = dict(spec=RATE_SPEC, stat={**RATE_STAT, "ci_low": 2.9, "ci_high": 3.4}, second=None, facts=facts,
              binding=bind_finding(RATE_SPEC, RATE_STAT, facts, GOOD_RATE), rev_checks=REV_CHECKS,
              receipts=[{"query_id": "q1", "query_hash": "a" * 64, "result_hash": "b" * 64}],
              manifest={"version": "m1"}, entry=_entry(), population={"check": "representative_population", "passed": True},
              caveats=[], confirmation=Confirmation(), replicated=False, reproducible=True, review_score=0.8,
              family_size=12, alpha=0.05, origin="heuristic", iteration=1, parent=None, claim_meta={"subject": "P1"})
    kw.update(over)
    return B.assemble(**kw)


def test_bundle_carries_every_evidence_dimension():
    b = _assemble()
    assert b.validation.state == "exploratory" and b.validation.label == "discovery" and not b.validation.missing_evidence
    m = b.method
    assert m["effect"]["value"] == 0.21 and m["uncertainty"]["low"] == 2.9
    assert m["sample_sizes"]["n"] == 20000 and m["sample_sizes"]["by_group"]["P4"] == 9000
    assert m["multiple_testing"] == {"procedure": "benjamini_hochberg", "family": "primary tests of this run",
                                     "family_size": 12, "p": 1e-14, "q": 1e-12, "alpha": 0.05}
    assert m["selection"]["top_group_selected_post_hoc"] is True and m["power"]["assessed"] is True
    assert b.data["entry"]["version"] and b.data["queries"][0]["result_hash"]
    assert b.review_score.calibrated is False and "not a calibrated probability" in b.review_score.note
    EvidenceBundle.model_validate(b.model_dump(mode="json"))


@pytest.mark.parametrize("over,missing", [
    ({"receipts": []}, "data.queries"),
    ({"receipts": [{"query_id": "q1", "result_hash": None}]}, "data.queries"),
    ({"entry": None}, "data.manifest"),
    ({"facts": []}, "claim.facts"),
    ({"stat": {**RATE_STAT, "effect_size": None}}, "method.effect_size"),
    ({"stat": {**RATE_STAT, "n": 0, "groups": []}}, "method.sample_sizes"),
    ({"stat": {**RATE_STAT, "ci_low": None}}, "method.uncertainty"),
    ({"rev_checks": [c for c in REV_CHECKS if c["check"] != "method_fit"]}, "method.assumptions"),
    ({"rev_checks": [c for c in REV_CHECKS if c["check"] != "representative_population"]}, "data.population"),
])
def test_missing_evidence_is_refused(over, missing):
    b = _assemble(**over)
    assert missing in b.validation.missing_evidence
    assert b.validation.state == "insufficient_evidence" and b.validation.label == "discovery"
    check = next(c for c in b.validation.checks if c.check == "evidence_complete")
    assert check.outcome == "fail" and missing in check.reason


def test_a_method_may_declare_uncertainty_optional_and_a_second_method_interval_counts():
    assert "method.uncertainty" not in _assemble(stat={**RATE_STAT, "ci_low": None}, optional=("uncertainty",)).validation.missing_evidence
    b = _assemble(stat={**RATE_STAT, "ci_low": None}, second={"test": "grouped_logistic", "ci_low": 2.0, "ci_high": 4.0})
    assert b.method["uncertainty"]["source"] == "second_method" and not b.validation.missing_evidence


def test_failed_checks_map_to_invalid_or_inconclusive():
    fail = lambda name: [{**c, "passed": c["check"] != name} for c in REV_CHECKS]  # noqa: E731
    assert _assemble(rev_checks=fail("fact_binding")).validation.state == "invalid"
    assert _assemble(rev_checks=fail("second_method")).validation.state == "inconclusive"
    assert _assemble(rev_checks=fail("data_version_stable")).validation.state == "inconclusive"


def test_power_at_the_materiality_threshold_grows_with_sample_size():
    small = B.power_at_threshold({"highlights": {"baseline_rate": 0.1, "top_n": 200, "baseline_n": 200}})
    large = B.power_at_threshold({"highlights": {"baseline_rate": 0.1, "top_n": 20000, "baseline_n": 20000}})
    assert small["assessed"] and small["power"] < 0.3 < 0.99 < large["power"] and not small["adequate"] and large["adequate"]
    assert B.power_at_threshold(TREND_STAT) == {"assessed": False, "reason": "no closed-form power rule for this result shape"}


# ------------------------------------------------------------------------------------ discovery vs confirmation
def test_confirmed_cannot_be_constructed_without_a_passing_rule():
    with pytest.raises(ValidationError):
        Validation(state="confirmed", label="confirmation")
    with pytest.raises(ValidationError):
        Validation(state="exploratory", label="confirmation")
    with pytest.raises(ValidationError):
        Validation(state="confirmed", label="discovery", confirmation=Confirmation(rule="holdout_partition", passed=True))
    Validation(state="confirmed", label="confirmation", confirmation=Confirmation(rule="holdout_partition", passed=True))


def test_a_discovery_is_exploratory_even_when_every_check_passes():
    c = C.evaluate(verified=True, origin="agent", top="P1", direction="higher")
    assert not c.passed and [r["passed"] for r in c.evaluated] == [False, False]
    assert _assemble(confirmation=c).validation.state == "exploratory"


def test_fresh_snapshot_replication_confirms_only_on_a_new_data_version():
    old, new = _entry(), _entry(content_fingerprint="d" * 64, rows_staged=19000)
    prior = {"run_id": "r1", "version": old.version, "top": "P1", "direction": "higher"}
    ok = C.evaluate(verified=True, origin="carried", top="P1", direction="higher", prior=prior, current=new)
    assert ok.passed and ok.rule == "fresh_snapshot_replication"
    b = _assemble(confirmation=ok)
    assert (b.validation.state, b.validation.label) == ("confirmed", "confirmation")
    assert "share rows" in b.limits["confirmation_overlap"]
    reasons = {
        "same data version": dict(prior=prior, current=old, origin="registry"),
        "selected in this run": dict(prior=prior, current=new, origin="agent"),
        "no data-version manifest": dict(prior={**prior, "version": None}, current=new, origin="carried"),
        "top group changed": dict(prior={**prior, "top": "P2"}, current=new, origin="carried"),
        "direction changed": dict(prior={**prior, "direction": "lower"}, current=new, origin="carried"),
        "no fixed version": dict(prior=prior, current=M.entry_from("src_x.incident", "s1", "pushdown"), origin="carried"),
        "no earlier verified finding": dict(prior=None, current=new, origin="carried"),
    }
    for why, kw in reasons.items():
        c = C.evaluate(verified=True, top="P1", direction="higher", **kw)
        assert not c.passed and why in c.evaluated[1]["reason"], (why, c.evaluated)
    assert not C.evaluate(verified=False, origin="carried", top="P1", direction="higher", prior=prior, current=new).passed
    assert C.is_replication("registry", prior, old, "P1") and not C.is_replication("registry", prior, new, "P1")
    assert _assemble(replicated=True).validation.state == "replicated"


def test_holdout_rule_requires_the_claim_locked_before_the_partition_is_read():
    h = {"partition": "hash(id) % 5 = 0", "claim_locked_at": "2026-09-26T10:00:00", "partition_accessed_at": "2026-09-26T10:05:00",
         "supported": True, "top": "P1", "direction": "higher"}
    assert C.evaluate(verified=True, origin="agent", top="P1", direction="higher", holdout=h).rule == "holdout_partition"
    for bad in ({"partition_accessed_at": "2026-09-26T09:00:00"}, {"supported": False}, {"top": "P4"}, {"direction": "lower"}):
        assert not C.evaluate(verified=True, origin="agent", top="P1", direction="higher", holdout={**h, **bad}).passed


# ------------------------------------------------------------------------------------ data-version manifest
def test_content_fingerprint_is_order_independent_and_content_sensitive():
    rows = [[1, "a", 2.5], [2, "b", None], [3, "c", 1.0]]

    def fp(rs):
        f = ContentFingerprint(["id", "s", "v"], ["integer", "text", "double precision"])
        for r in rs:
            f.add(list(r))
        return f.hexdigest()

    assert fp(rows) == fp(list(reversed(rows)))
    assert fp(rows) != fp(rows[:2]) and fp(rows) != fp([[1, "a", 2.5], [2, "b", None], [3, "c", 1.5]])
    assert fp(rows + rows[:1]) != fp(rows)  # duplicates count


def test_manifest_versions_follow_content_not_the_load():
    a, same = _entry(), _entry(load_id="load_2", staged_at="2026-09-27T10:00:00")
    assert a.immutable and a.version_basis == "content" and a.version == same.version
    assert M.changed(a, same) is None
    changed = _entry(content_fingerprint="d" * 64, rows_staged=19000)
    assert "rows 20000 -> 19000" in M.changed(a, changed) and "row content changed" in M.changed(a, changed)
    legacy = M.entry_from("src_x.incident", "s1", "staged", snapshot={"rows_staged": 5, "staged_at": "t1", "load_id": "l1"})
    restaged = M.entry_from("src_x.incident", "s1", "staged", snapshot={"rows_staged": 5, "staged_at": "t2", "load_id": "l2"})
    assert legacy.version_basis == "load_metadata" and "re-staged" in M.changed(legacy, restaged)
    push = M.entry_from("src_x.incident", "s1", "pushdown")
    assert not push.immutable and push.version is None and push.observed_at and M.changed(push, push) is None
    m1, m2 = M.build([a]), M.build([same])
    assert m1.version == m2.version and M.build([changed]).version != m1.version and m1.entry("src_x.incident") == a


def test_snapshot_change_marks_a_finding_stale():
    bundle = _assemble().model_dump(mode="json")
    assert M.freshness(bundle, {"src_x.incident": _entry(load_id="load_9")}).state == "current"
    stale = M.freshness(bundle, {"src_x.incident": _entry(content_fingerprint="e" * 64)})
    assert stale.state == "stale" and stale.assets == ["src_x.incident"] and "row content changed" in stale.reason
    assert M.freshness({"data": {}}, {}).state == "unknown"


# ------------------------------------------------------------------------------------ legacy badges
def _migration():
    path = ROOT / "migrations" / "versions" / "0030_typed_evidence.py"
    spec = importlib.util.spec_from_file_location("mig0030", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LEGACY_CASES = [
    ("verified", True, 0.85, {"evaluate": [{"check": "sample_size", "passed": True, "detail": "n=900"}],
                              "verify": {"reproducible": True}}),
    ("failed_verification", False, 0.3, {"evaluate": [{"check": "second_method", "passed": False, "detail": "disagrees"}]}),
    ("rejected", False, 0.2, {}),
    ("draft", False, 0.0, None),
    ("verified", False, 0.5, {}),  # inconsistent legacy row: status says verified, flag does not
]


@pytest.mark.parametrize("status,verified,confidence,verification", LEGACY_CASES)
def test_legacy_badges_map_onto_the_evidence_model(status, verified, confidence, verification):
    bundle, state = B.legacy_bundle(status, verified, confidence, verification)
    assert _migration().legacy_bundle(status, verified, confidence, verification) == (bundle, state)  # migration == code
    parsed = EvidenceBundle.model_validate(bundle)
    assert parsed.validation.state == state != "confirmed" and parsed.validation.label != "confirmation"
    assert parsed.legacy == {"status": status, "verified": bool(verified), "confidence": confidence, "verifier_version": "rev.v1",
                             "equivalent": parsed.legacy["equivalent"]}
    assert parsed.review_score.value == confidence and parsed.review_score.calibrated is False
    expected = {"verified": "legacy" if verified else "legacy", "failed_verification": "inconclusive", "rejected": "invalid",
                "draft": "legacy"}[status]
    assert state == expected
    if status == "verified" and verified:
        assert parsed.legacy["equivalent"] == "exploratory" and parsed.validation.reproducible is True
        assert parsed.validation.checks[0].outcome == "pass"
