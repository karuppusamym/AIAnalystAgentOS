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
