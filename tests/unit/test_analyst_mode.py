"""Ask analyst mode (no services): the rules planner (shapes, dedup, cap), facts / series / two-period
drivers computed in code, the step checks, the numbers guard on a synthesis, the model gates (planning
and synthesis are validated or dropped) and a whole turn over a fake single-question Ask: partial
failure, clarify with an assumption, the budget stop and the headline mirror."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.agents import analyst
from analystos.core.errors import BudgetExceeded, SQLRejected
from analystos.services import ask as ask_svc
from analystos.skills import result_facts as rf

CATALOG = {"tables": [{"fq": "stg.incident", "entity": "incident", "entity_words": ["incident"], "has_time": True,
                       "dimensions": [{"name": "priority", "label": "priority", "words": ["priority"], "distinct": 5},
                                      {"name": "category", "label": "category", "words": ["category"], "distinct": 8},
                                      {"name": "state", "label": "state", "words": ["state"], "distinct": 6}],
                       "measures": [{"name": "reassignment_count", "label": "reassignment count",
                                     "words": ["reassignment", "count"]}]}]}


def _plan(q, catalog=CATALOG):
    cleaned, assumed = analyst.split_assumption(q)
    return analyst.plan_rules(cleaned, catalog, assumed)


# ------------------------------------------------------------------------------------ planner
def test_a_simple_question_is_one_step_with_the_question_unchanged():
    for q, kind in [("How many incidents by priority?", "breakdown"), ("incidents over time", "trend"),
                    ("top 5 categories by incidents", "ranking"), ("how many incidents", "question")]:
        plan = _plan(q)
        assert [s["kind"] for s in plan["steps"]] == [kind] and plan["steps"][0]["question"] == q.rstrip("?")
        assert plan["origin"] == "rules" and plan["steps"][0]["n"] == 1


def test_two_dimensions_are_a_total_and_one_breakdown_each():
    plan = _plan("incidents by priority and state")
    assert [(s["kind"], s["question"]) for s in plan["steps"]] == [
        ("total", "how many incidents"), ("breakdown", "incidents by priority"), ("breakdown", "incidents by state")]


def test_a_numeric_measure_total_and_the_time_range_carry_into_every_step():
    plan = _plan("total reassignment count by category and priority in 2025")
    assert [s["question"] for s in plan["steps"]] == ["total reassignment count in 2025",
                                                      "total reassignment count by category in 2025",
                                                      "total reassignment count by priority in 2025"]


def test_a_change_question_is_two_periods_then_drivers_over_catalog_dimensions():
    plan = _plan("Why did incidents increase last month?")
    kinds = [s["kind"] for s in plan["steps"]]
    assert kinds == ["comparison", "drivers", "drivers"]
    assert plan["steps"][0]["question"] == "incidents last month compared with the month before, one column per period"
    assert "by priority" in plan["steps"][1]["question"] and "by category" in plan["steps"][2]["question"]
    assert plan["assumptions"] == []


def test_a_change_without_a_period_states_its_assumption():
    plan = _plan("why did incidents drop?")
    assert "the latest month" in plan["steps"][0]["question"] and plan["assumptions"]
    assert "question names no period" in plan["assumptions"][0]


def test_this_month_vs_last_month_is_a_two_period_comparison_with_named_drivers():
    plan = _plan("Compare incidents this month vs last month by state")
    assert [s["kind"] for s in plan["steps"]] == ["comparison", "drivers"]
    assert plan["steps"][1]["question"] == "incidents by state this month compared with last month, one column per period"


def test_without_a_catalog_a_change_question_is_only_the_comparison():
    plan = _plan("why did incidents rise last week?", catalog={"tables": []})
    assert [s["kind"] for s in plan["steps"]] == ["comparison"]


def test_plans_are_capped_at_four_steps_and_deduplicated():
    plan = _plan("incidents by priority, state, category, group and region")
    assert len(plan["steps"]) == analyst.MAX_STEPS
    assert [s["n"] for s in plan["steps"]] == [1, 2, 3, 4]
    dup = analyst._finalise({"steps": [analyst._step("question", "a", "Incidents by X?"),
                                       analyst._step("question", "b", "incidents by x")]}, [])
    assert len(dup["steps"]) == 1


def test_an_accepted_assumption_is_added_to_each_step():
    plan = _plan("incidents over time\n\nProceed with this assumption: in the last 12 months")
    assert plan["steps"][0]["question"] == "incidents over time in the last 12 months"
    assert plan["assumptions"] == ["in the last 12 months"]


def test_a_second_question_joined_by_and_stays_one_step_and_looks_compound():
    q = "incidents by priority and average reassignment count by state"
    plan = _plan(q)
    assert len(plan["steps"]) == 1 and analyst.looks_compound(q)


def test_model_plans_are_validated():
    assert analyst.validate_model_plan({"steps": [{"goal": "g", "question": "a"}]}) is None
    assert "1 to 4" in analyst.validate_model_plan({"steps": [{"question": f"q{i}"} for i in range(5)]})
    assert "no question" in analyst.validate_model_plan({"steps": [{"question": " "}]})
    assert "repeats" in analyst.validate_model_plan({"steps": [{"question": "A?"}, {"question": "a"}]})
    assert analyst.validate_model_plan({"plan": []}) is not None


# ------------------------------------------------------------------------------------ facts
def test_facts_totals_labels_identifiers_and_pre_aggregated_columns():
    cols = ["priority", "customer_id", "n", "avg_hours", "breach_rate"]
    rows = [["P1", 7, 10, 2.5, 0.2], ["P2", 8, 30, 4.0, 0.1], ["P3", 9, 5, 1.0, 0.4]]
    f = rf.step_facts(cols, rows, row_count=3)
    by = {m["column"]: m for m in f["measures"]}
    assert by["n"]["total"] == 45 and by["n"]["max_label"] == "P2" and by["n"]["min_label"] == "P3" and by["n"]["avg"] == 15
    assert "total" not in by["avg_hours"] and "total" not in by["breach_rate"] and by["breach_rate"]["pre_aggregated"]
    assert f["identifiers"] == [{"column": "customer_id", "distinct": 3}]
    assert "customer_id" not in by and f["label_columns"] == ["priority"]


def test_a_numeric_grouping_is_a_dimension_named_by_its_column():
    sql = 'SELECT "priority" AS "priority", COUNT(*) AS "n" FROM stg.incident GROUP BY "priority"'
    dims = rf.grouped_columns(sql)
    f = rf.step_facts(["priority", "n"], [[4, 70], [1, 5]], dimensions=dims)
    assert dims == {"priority"} and [m["column"] for m in f["measures"]] == ["n"]
    assert f["measures"][0]["max_label"] == "priority 4" and f["label_columns"] == ["priority"]
    assert rf.numbers_bound("Priority 4 leads with 70 of 75 (step 1).", [70, 75], labels=["priority 4"], steps=[1])["ok"]


def test_truncation_is_stated_first():
    f = rf.step_facts(["x", "n"], [["a", 1], ["b", 2]], row_count=500, truncated=True)
    assert f["truncated"] and f["statements"][0].startswith("The result is truncated")


def test_a_single_row_is_a_value():
    f = rf.step_facts(["incident_count"], [[42]], row_count=1)
    assert f["values"] == {"incident_count": 42} and f["statements"] == ["Number of incidents: 42."]  # names for people, values untouched


# ------------------------------------------------------------------------------------ series
def _months(values):
    return [[f"2025-{i + 1:02d}-01T00:00:00", v] for i, v in enumerate(values)]


def test_series_slope_share_of_mean_and_direction():
    s = rf.series_analysis(["month", "n"], _months([10, 12, 14, 16, 18, 20]))
    assert s["slope_per_period"] == 2 and s["direction"] == "increasing" and s["points"] == 6
    assert s["slope_share_of_mean"] == pytest.approx(2 / 15, abs=1e-6) and s["grain"] == "month"
    flat = rf.series_analysis(["month", "n"], _months([100, 100.2, 99.9, 100.1, 100.0, 100.1]))
    assert flat["direction"] == "flat"
    assert rf.series_analysis(["month", "n"], _months([1, 2, 3, 4])) is None  # fewer than 5 points


def test_series_flags_robust_anomalies_and_missing_periods():
    rows = _months([10, 11, 10, 12, 11, 60, 10, 11])
    del rows[2]
    s = rf.series_analysis(["month", "n"], rows)
    assert [a["period"] for a in s["anomalies"]] == [rows[4][0]] and s["anomalies"][0]["z"] >= 3
    assert s["missing_periods"] == ["2025-03-01"]


# ------------------------------------------------------------------------------------ two periods
def test_driver_math_pct_change_with_zero_previous_and_shares_beyond_100_percent():
    cols = ["category", "previous_month", "this_month"]
    rows = [["Network", 10, 40], ["Hardware", 20, 5], ["Software", 0, 5], ["Email", 8, 0]]
    tp = rf.two_period(cols, rows)
    assert (tp["previous"], tp["current"], tp["change"]) == (38, 50, 12)
    d = {r["member"]: r for r in tp["drivers"] + tp["offsets"]}
    assert d["Network"]["share_of_change"] == 2.5  # not clamped: the offsets make it exceed 100%
    assert d["Software"]["pct_change"] is None and d["Software"]["appeared"]
    assert d["Hardware"]["pct_change"] == -0.75 and [r["member"] for r in tp["offsets"]] == ["Hardware", "Email"]
    assert [r["member"] for r in tp["drivers"]] == ["Network", "Software"]
    assert tp["appeared"] == ["Software"] and tp["disappeared"] == ["Email"]


def test_long_format_two_periods_pivot_and_nothing_moved_means_no_drivers():
    cols = ["month", "priority", "n"]
    rows = [["2025-05-01", "P1", 5], ["2025-05-01", "P2", 3], ["2025-06-01", "P1", 7], ["2025-06-01", "P2", 3]]
    tp = rf.two_period(cols, rows)
    assert tp["previous_period"] == "2025-05-01" and tp["change"] == 2 and tp["drivers"][0]["member"] == "P1"
    still = rf.two_period(["priority", "last_month", "this_month"], [["P1", 3, 3], ["P2", 4, 4]])
    assert still["change"] == 0 and "drivers" not in still


def test_pre_aggregated_period_columns_are_not_decomposed():
    assert rf.two_period(["priority", "avg_prev", "avg_curr"], [["P1", 1.0, 2.0], ["P2", 1.0, 3.0]]) is None


# ------------------------------------------------------------------------------------ checks
def test_checks_flag_a_one_row_breakdown_and_a_share_above_100_percent():
    checks = rf.step_checks("breakdown", sql="SELECT 1", dialect="postgres", columns=["priority", "share_pct"],
                            rows=[["P1", 130.0]], row_count=1, truncated=False)
    status = {c["code"]: c["status"] for c in checks}
    assert status["single_row_breakdown"] == "suspect" and status["share_range"] == "suspect"
    assert status["empty_result"] == "pass" and status["truncation"] == "pass"


def test_checks_flag_missing_periods_and_a_comparison_without_two_periods():
    series = {"missing_periods": ["2025-03-01"]}
    checks = rf.step_checks("comparison", sql=None, dialect="postgres", columns=["n"], rows=[[1], [2]], row_count=2,
                            truncated=False, series=series, comparison=None)
    status = {c["code"]: c["status"] for c in checks}
    assert status["missing_periods"] == "suspect" and status["two_periods"] == "suspect"


# ------------------------------------------------------------------------------------ numbers guard
def test_numbers_bound_accepts_stated_precision_and_percentages_of_fractions():
    ok = rf.numbers_bound("Incidents rose to 1,235 (step 1). That is +12.5% (step 1).", [1234.6, 0.125], steps=[1])
    assert ok["ok"] and ok["numbers"] == 2


def test_numbers_bound_rejects_invented_numbers_uncited_sentences_and_unknown_steps():
    assert not rf.numbers_bound("Incidents rose to 1,300 (step 1).", [1234.6])["ok"]
    assert "cites no step" in rf.numbers_bound("Incidents rose to 1,235.", [1234.6])["problems"][0]
    assert not rf.numbers_bound("Incidents rose to 1,235 (step 3).", [1234.6], steps=[1, 2])["ok"]
    assert rf.numbers_bound("No number here.", [])["ok"]


def test_numbers_bound_masks_labels_and_needs_a_direction_for_a_negative_fact():
    assert rf.numbers_bound("Period 2025-03 peaked at 60 (step 2).", [60], labels=["2025-03"], steps=[2])["ok"]
    assert rf.numbers_bound("Hardware fell by 15 (step 2).", [-15], steps=[2])["ok"]
    assert not rf.numbers_bound("Hardware grew by 15 (step 2).", [-15], steps=[2])["ok"]


# ------------------------------------------------------------------------------------ a whole turn
class Router:
    def __init__(self, mode="auto"):
        self._mode, self.skips = mode, []

    def mode(self, purpose):
        return self._mode

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append(purpose)


def _ctx(router=None):
    stages = []
    ctx = SimpleNamespace(scope=SimpleNamespace(source_dialects={"s": "postgres"}, assets=["stg.incident"]), router=router,
                          policy=None, call_ctx=lambda **_: None, say=lambda *a, **k: None,
                          on_stage=lambda key, text, data: stages.append((key, text, data)))
    return ctx, stages


def _answer(columns, rows, sql="SELECT 1"):
    return {"status": "answered", "answered_by": "rules", "sql": sql, "chart": {"type": "bar"}, "model": None,
            "route": "tool", "decisions": [{"purpose": "ask_route"}], "attempts": [],
            "result": {"query_id": "q", "columns": columns, "rows": rows, "row_count": len(rows), "truncated": False,
                       "referenced_assets": ["stg.incident"], "result_hash": "h"}}


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(analyst, "planner_catalog", lambda ctx: CATALOG)


def _run(ctx, question, fake):
    return analyst.run(ctx, question, ask_fn=fake, finish=ask_svc._finish, refuse=ask_svc.refusal_for)


def test_a_turn_runs_every_step_keeps_a_failed_one_and_mirrors_the_headline(catalog):
    asked = []

    def fake(ctx, q, parameters=None):
        asked.append(q)
        ctx.on_stage("execute", "Running it through the query gateway", {})
        if "by priority" in q:
            raise SQLRejected("column prio does not exist")
        if "by category" in q:
            return _answer(["category", "previous_month", "this_month"], [["Network", 10, 40], ["Hardware", 20, 5]])
        return _answer(["previous_month", "this_month"], [[30, 45]])

    ctx, stages = _ctx()
    out = _run(ctx, "Why did incidents increase last month?", fake)
    a = out["analysis"]
    assert out["status"] == "answered" and out["route"] == "analyst" and len(asked) == 3
    assert [s["status"] for s in a["steps"]] == ["answered", "refused", "answered"]
    assert a["steps"][1]["refusal"]["kind"] == "sql_rejected"
    assert a["headline_step"] == 1 and out["result"]["rows"] == [[30, 45]] and out["sql"] == "SELECT 1"
    assert a["steps"][0]["comparison"]["change"] == 15 and a["steps"][2]["drivers"]["drivers"][0]["member"] == "Network"
    syn = a["synthesis"]
    assert syn["origin"] == "template" and syn["citations"] == [1, 2, 3]
    assert syn["answer"].startswith("The total went from 30 to 45 (+15, +50.0%) (step 1)")
    assert any("could not be answered" in c and "(step 2)" in c for c in syn["caveats"])
    values, labels = analyst.bound_inputs(a["steps"], a["plan"], {})
    assert rf.numbers_bound(syn["text"], values, labels=labels, steps=[1, 3])["ok"]  # the template binds
    texts = [t for _, t, _ in stages]
    assert texts[0].startswith("Planned 3 steps") and "Step 2 of 3: Running it through the query gateway" in texts
    assert [d["step"] for d in out["decisions"]] == [1, 3]
    assert out["explanation"] == syn["text"] and len(a["follow_ups"]) <= 3 and a["follow_ups"]


def test_a_step_that_needs_detail_makes_the_turn_clarify_with_an_assumption(catalog):
    def fake(ctx, q, parameters=None):
        return {"status": "clarify", "explanation": "Say which time column.", "missing": [{"name": "time"}],
                "suggestions": ["incidents by month of opened at"]}

    ctx, _ = _ctx()
    out = _run(ctx, "incidents over time", fake)
    assert out["status"] == "clarify" and out["assumption"] == "in the last 12 months" and out["clarify_step"] == 1
    status, refusal = ask_svc._finish(out)
    assert status == "clarify" and refusal["details"]["assumption"] == "in the last 12 months"
    assert refusal["details"]["suggestions"] == ["incidents by month of opened at"]
    assert out["analysis"]["synthesis"] is None and out["analysis"]["steps"][0]["status"] == "clarify"


def test_a_used_up_budget_skips_the_remaining_steps_and_nothing_answered_is_a_refusal(catalog):
    calls = []

    def fake(ctx, q, parameters=None):
        calls.append(q)
        raise BudgetExceeded("Ask query budget exhausted", details={"scope": "user", "used": 60, "limit": 60})

    ctx, _ = _ctx()
    out = _run(ctx, "incidents by priority and state", fake)
    assert len(calls) == 1 and out["status"] == "refused" and out["refusal"]["kind"] == "budget_exceeded"
    assert [s["status"] for s in out["analysis"]["steps"]] == ["refused", "skipped", "skipped"]


def test_the_planning_model_is_asked_only_for_a_compound_looking_one_step_plan(catalog, monkeypatch):
    from analystos.agents import common

    calls = []

    def llm_json(ctx, purpose, prompt, payload, validate=None, **kw):
        calls.append(purpose)
        return {"approach": "two questions", "steps": [{"goal": "Count", "question": "incidents by priority"},
                                                       {"goal": "Average", "question": "average reassignment count by state"}]}, "m"

    monkeypatch.setattr(common, "llm_json", llm_json)
    ctx, _ = _ctx(Router("auto"))
    plan = analyst.make_plan(ctx, "incidents by priority and average reassignment count by state", CATALOG)
    assert calls == ["analyst_planning"] and plan["origin"] == "model" and len(plan["steps"]) == 2
    simple = analyst.make_plan(ctx, "incidents by priority", CATALOG)
    assert calls == ["analyst_planning"] and simple["origin"] == "rules" and "analyst_planning" in ctx.router.skips

    monkeypatch.setattr(common, "llm_json", lambda *a, **k: ({"steps": [{"question": ""}]}, "m"))
    bad = analyst.make_plan(ctx, "incidents by priority and average reassignment count by state", CATALOG)
    assert bad["origin"] == "rules" and bad["model_rejected"]


def test_a_synthesis_model_is_kept_only_when_its_numbers_bind(catalog, monkeypatch):
    from analystos.agents import common

    entries = [{"n": 1, "kind": "breakdown", "goal": "Break incidents down", "status": "answered", "sql": "SELECT 1",
                "result": {"columns": ["priority", "n"], "rows": [["P1", 10], ["P2", 30]]},
                **analyst.analyse("breakdown", sql="SELECT 1", dialect="postgres",
                                  result={"columns": ["priority", "n"], "rows": [["P1", 10], ["P2", 30]], "row_count": 2})}]
    plan = {"steps": [{"n": 1, "kind": "breakdown", "goal": "Break incidents down", "question": "q"}], "assumptions": []}
    auto, _ = _ctx(Router("auto"))
    assert analyst.synthesize(auto, "q", plan, entries, 1)["origin"] == "template" and "analyst_synthesis" in auto.router.skips

    always, _ = _ctx(Router("always"))
    monkeypatch.setattr(common, "llm_json", lambda *a, **k: ({"text": "P2 leads with 30 of 40 incidents (step 1)."}, "m"))
    good = analyst.synthesize(always, "q", plan, entries, 1)
    assert good["origin"] == "model" and good["citations"] == [1]
    monkeypatch.setattr(common, "llm_json", lambda *a, **k: ({"text": "P2 leads with 75% of 44 incidents (step 1)."}, "m"))
    bad = analyst.synthesize(always, "q", plan, entries, 1)
    assert bad["origin"] == "template" and "44" in bad["rejected"]


def test_rerun_entry_recomputes_facts_and_feeds_the_old_value_to_the_magnitude_check():
    old = {"n": 1, "kind": "question", "goal": "g", "question": "q", "status": "answered", "sql": "SELECT 1",
           "answered_by": "semantic", "governance": "governed",
           "result": {"columns": ["n"], "rows": [[10]], "row_count": 1, "query_id": "q1"}}
    new = analyst.rerun_entry(old, "SELECT 2", {"columns": ["n"], "rows": [[1000]], "row_count": 1, "query_id": "q2"},
                              "postgres", edited=True, at="t")
    assert new["facts"]["values"] == {"n": 1000} and new["governance"] == "ad_hoc" and new["answered_by"] == "sql"
    assert new["rerun"]["previous_query_id"] == "q1" and new["rerun"]["edited"]
    assert {c["code"]: c["status"] for c in new["checks"]}["magnitude"] == "suspect"


def test_analyst_mode_rejects_an_explicit_metric_query(catalog):
    from analystos.core.errors import InvalidInput

    ctx, _ = _ctx()
    with pytest.raises(InvalidInput):
        analyst.run(ctx, "q", parameters={"semantic_query": {}}, ask_fn=lambda *a, **k: {}, finish=ask_svc._finish,
                    refuse=ask_svc.refusal_for)


def test_the_new_purposes_are_rules_first_and_have_prompts():
    from pathlib import Path

    import yaml

    from analystos.agents.prompts import prompt
    from analystos.contracts.platform import DETERMINISTIC_CAPABLE

    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "models.yaml").read_text(encoding="utf-8"))
    for purpose in (analyst.PLANNING_PURPOSE, analyst.SYNTHESIS_PURPOSE):
        ladder = cfg["ladders"][purpose]
        assert ladder.index("rules") < ladder.index("llm_small") and purpose in DETERMINISTIC_CAPABLE
        assert prompt(f"{purpose}.v1")
