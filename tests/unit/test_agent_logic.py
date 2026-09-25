from types import SimpleNamespace

from analystos.agents.critic import CAUSAL
from analystos.agents.insight import _guard, facts_for, template_text
from analystos.agents.investigator import validate_spec, with_constraints
from analystos.contracts.analysis import AnalysisSpec
from analystos.contracts.policy import DataScope
from analystos.runtime.plan import base_plan, dep_satisfied, plan_hash

SCOPE = DataScope(workspace_id="ws", user_id="u", role="owner", assets=["src_x.incident"],
                  asset_sources={"src_x.incident": "s1"},
                  columns={"src_x.incident": ["made_sla", "reassignment_count", "priority", "opened_at", "resolved_at",
                                              "assignment_group_name", "caller_id", "sys_id"]},
                  denied_columns=["src_x.incident.caller_id"])
TYPES = {"src_x.incident": {"made_sla": "boolean", "reassignment_count": "numeric", "priority": "categorical",
                            "opened_at": "datetime", "resolved_at": "datetime", "assignment_group_name": "categorical",
                            "caller_id": "id", "sys_id": "id"}}


def spec(**kw):
    base = {"method": "rate_by_segment", "asset": "src_x.incident",
            "outcome": {"type": "equals", "column": "made_sla", "value": False},
            "segment": {"type": "bucket", "column": "reassignment_count", "edges": [0, 1, 2, 3]}}
    base.update(kw)
    return AnalysisSpec.model_validate(base)


def test_valid_spec_passes():
    assert validate_spec(spec(), SCOPE, TYPES) == []


def test_out_of_scope_and_restricted_columns_rejected():
    assert "not in the authorized scope" in validate_spec(spec(asset="public.secrets"), SCOPE, TYPES)[0]
    errs = validate_spec(spec(segment={"type": "column", "column": "caller_id"}), SCOPE, TYPES)
    assert any("restricted" in e for e in errs)
    assert any("unknown column" in e for e in validate_spec(spec(segment={"type": "column", "column": "nope"}), SCOPE, TYPES))


def test_method_type_compatibility():
    assert any("boolean outcome" in e for e in validate_spec(spec(outcome={"type": "column", "column": "priority"}), SCOPE, TYPES))
    assert any("bucketed" in e for e in validate_spec(spec(segment={"type": "column", "column": "reassignment_count"}), SCOPE, TYPES))
    assert any("identifier" in e for e in validate_spec(spec(segment={"type": "column", "column": "sys_id"}), SCOPE, TYPES))
    trend = spec(method="trend", outcome=None, segment=None, time={"type": "column", "column": "opened_at"})
    assert any("date_trunc" in e for e in validate_spec(trend, SCOPE, TYPES))


def test_user_redirect_filters_apply_to_matching_asset_only():
    s = with_constraints(spec(), {"filters": [{"asset": "src_x.incident", "column": "priority", "op": "=", "value": "1"},
                                              {"asset": "other.t", "column": "x", "op": "=", "value": 1}]})
    assert [(f.column, f.origin) for f in s.filters] == [("priority", "user_redirect")]


def test_narrative_guard_blocks_invented_numbers():
    stat = {"test": "chi2", "n": 20000, "p_adjusted": 1e-12, "effect_size": 0.21, "effect_label": "cramers_v",
            "highlights": {"top_segment": "3+", "top_rate": 0.412, "baseline_segment": "0", "baseline_rate": 0.131, "rate_ratio": 3.14}}
    facts = facts_for(stat, spec().model_dump())
    assert _guard("Incidents reassigned 3+ times breach SLA 41.2% of the time vs 13.1% (3.1x).", facts)
    assert not _guard("Breach rate is 57% for reassigned incidents.", facts)
    title, text = template_text(stat, spec().model_dump())
    assert "41.2%" in text and "13.1%" in text


def test_causal_language_detector():
    assert CAUSAL.search("Reassignment drives SLA breach")
    assert not CAUSAL.search("Reassignment is associated with SLA breach")


def test_plan_shape_and_hash_binding():
    p3 = base_plan("objective", autonomy_level=3)
    p2 = base_plan("objective", autonomy_level=2)
    assert p3["steps"][0]["key"] == "context" and p2["steps"][0]["key"] == "plan_approval"
    h1 = plan_hash(p3, constraints={}, scope_hash="a", plan_version=1)
    assert h1 != plan_hash(p3, constraints={"filters": [1]}, scope_hash="a", plan_version=1)
    assert h1 != plan_hash(p3, constraints={}, scope_hash="b", plan_version=1)
    assert h1 != plan_hash(p3, constraints={}, scope_hash="a", plan_version=2)


def test_wildcard_dependencies():
    T = lambda s, opt=False: SimpleNamespace(status=s, input={"optional": opt})  # noqa: E731
    tasks = {"hypotheses": T("COMPLETED"), "test:H-1": T("COMPLETED"), "test:H-2": T("RUNNING"), "followups:1": T("NEW")}
    assert dep_satisfied("hypotheses", tasks)
    assert not dep_satisfied("test:*", tasks)
    tasks["test:H-2"] = T("FAILED", True)
    assert dep_satisfied("test:*", tasks)
    assert not dep_satisfied("followups:*", tasks)
    assert dep_satisfied("nothing:*", tasks)


def test_wildcard_ignores_tasks_waiting_on_the_waiter():
    T = lambda s, deps=(): SimpleNamespace(status=s, input={}, depends_on=list(deps))  # noqa: E731
    tasks = {"followups:1": T("NEW", ["test:*"]), "test:H-1": T("COMPLETED"), "test:H-9": T("NEW", ["followups:1"])}
    assert dep_satisfied("test:*", tasks, "followups:1")
    assert not dep_satisfied("test:*", tasks, "insights")


def test_jsonb_serializer_handles_numpy_and_non_finite():
    import json

    import numpy as np

    from analystos.db.base import json_dumps

    assert json.loads(json_dumps({"a": np.bool_(True), "b": float("nan"), "c": [np.float64("inf"), np.int64(2)]})) == \
        {"a": True, "b": None, "c": [None, 2]}


def test_metric_duplicates_ignore_quoting_and_case():
    from analystos.agents.semantic import _normalize

    assert _normalize('AVG("reassignment_count")', "postgres") == _normalize("avg(reassignment_count)", "postgres")
    assert _normalize("AVG(a)", "postgres") != _normalize("SUM(a)", "postgres")


def test_template_keeps_acronyms():
    stat = {"highlights": {"top_segment": "3+", "top_rate": 0.3, "baseline_segment": "1", "baseline_rate": 0.1, "rate_ratio": 3.0}}
    title, _ = template_text(stat, spec(outcome={"type": "equals", "column": "made_sla", "value": False, "label": "missed SLA"}).model_dump())
    assert title.startswith("Missed SLA")


def test_finalize_waits_for_the_analysis_even_when_publication_is_skipped():
    p = base_plan("objective", autonomy_level=3)
    finalize = next(s for s in p["steps"] if s["key"] == "finalize")
    assert "visualize" in finalize["depends_on"]


def test_claim_identity_distinguishes_driver_sets_and_filters():
    from analystos.services.changes import claim_key

    dm = lambda cols, filters=(): {"method": "driver_model", "outcome": {"column": "made_sla"},  # noqa: E731
                                   "drivers": [{"column": c} for c in cols], "filters": list(filters)}
    hl = {"top_driver": "reassignment_count"}
    assert claim_key(dm(["a", "b"]), hl) == claim_key(dm(["b", "a"]), hl)
    assert claim_key(dm(["a", "b"]), hl) != claim_key(dm(["a", "c"]), hl)
    assert claim_key(dm(["a"], [{"column": "priority", "op": "=", "value": 1}]), hl) != claim_key(dm(["a"]), hl)


def test_driver_model_title_names_the_driver_and_effect():
    from analystos.contracts.analysis import AnalysisSpec as Spec

    s = Spec.model_validate({"method": "driver_model", "asset": "s.orders",
                             "outcome": {"type": "is_true", "column": "returned", "label": "returned"},
                             "drivers": [{"type": "column", "column": "channel"}, {"type": "column", "column": "sales_region"}]})
    stat = {"highlights": {"top_driver": "channel", "strongest_feature": "channel=marketplace", "strongest_odds_ratio": 3.25}}
    title, text = template_text(stat, s.model_dump())
    assert title == "Returned is driven mainly by channel" and "3.2x the odds" in text


def test_second_driver_model_on_same_outcome_is_a_duplicate():
    from analystos.agents.investigator import identity_keys

    base = {"method": "driver_model", "asset": "s.orders", "outcome": {"type": "is_true", "column": "returned"}}
    a = AnalysisSpec.model_validate({**base, "drivers": [{"type": "column", "column": "channel"},
                                                          {"type": "column", "column": "sales_region"}]})
    b = AnalysisSpec.model_validate({**base, "drivers": [{"type": "column", "column": "channel"},
                                                          {"type": "column", "column": "net_amount"}]})
    filtered = AnalysisSpec.model_validate({**base, "drivers": b.model_dump()["drivers"],
                                            "filters": [{"column": "channel", "op": "=", "value": "web"}]})
    assert identity_keys(a) & identity_keys(b) and not identity_keys(a) & identity_keys(filtered)


def test_diverse_top_covers_each_outcome_before_repeating_one():
    from analystos.agents.investigator import diverse_top

    def h(method, outcome, score):
        return {"spec": {"method": method, "outcome": {"column": outcome}}, "priority_score": score}

    accepted = [h("rate_by_segment", "returned", 9), h("rate_by_segment", "returned", 8), h("rate_by_segment", "returned", 7),
                h("numeric_by_segment", "net_amount", 5), h("numeric_by_segment", "shipping_days", 4)]
    picked = diverse_top(accepted, 3)
    assert {p["spec"]["outcome"]["column"] for p in picked} == {"returned", "net_amount", "shipping_days"}


def test_matrix_continuation_breaks_each_outcome_down_by_the_next_dimension():
    from analystos.agents.investigator import _matrix_continuations

    types = {"s.orders": {"channel": "categorical", "sales_region": "categorical", "customer_segment": "categorical",
                          "email": "categorical", "net_amount": "numeric", "notes_text": "categorical"}}
    tested = [{"spec": {"method": "numeric_by_segment", "asset": "s.orders", "outcome": {"type": "column", "column": "net_amount"},
                        "segment": {"type": "column", "column": "channel"}}},
              {"spec": {"method": "numeric_by_segment", "asset": "s.orders", "outcome": {"type": "column", "column": "net_amount"},
                        "segment": {"type": "column", "column": "sales_region"}}}]
    [p] = _matrix_continuations(tested, types, denied=["s.orders.email"])
    assert p["spec"]["segment"]["column"] == "customer_segment" and p["spec"]["outcome"]["column"] == "net_amount"
    assert _matrix_continuations([{"spec": {**tested[0]["spec"], "filters": [{"column": "channel", "op": "=", "value": "web"}]}}],
                                 types, denied=[]) == []  # drill-downs are not extended


def test_matrix_continuation_prefers_least_explored_business_measures():
    from analystos.agents.investigator import _matrix_continuations

    types = {"s.o": {"channel": "categorical", "region": "categorical", "segment": "categorical"}}

    def r(col, seg, typ="column"):
        return {"spec": {"method": "numeric_by_segment", "asset": "s.o", "outcome": {"type": typ, "column": col},
                         "segment": {"type": "column", "column": seg}}}

    results = [r("quantity", "channel"), r("net_amount", "channel"), r("returned", "channel"), r("returned", "region")]
    roles = {"s.o.quantity": "measure", "s.o.net_amount": "amount", "s.o.returned": "flag"}
    order = [p["spec"]["outcome"]["column"] for p in _matrix_continuations(results, types, [], roles)]
    assert order == ["net_amount", "quantity", "returned"]
