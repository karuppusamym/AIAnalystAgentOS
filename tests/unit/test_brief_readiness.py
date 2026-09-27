"""P4-04 on the in-memory control plane: the versioned workspace brief (origin, evidence, review state,
per-assertion versions; inferred assertions stay suggestions until reviewed or validated; a person's
decision is never overwritten), readiness assessments (per-check reasons, no averaged score; unsupported
and missing-label work cannot start), Start-work job kinds with reasons, and scoped memory."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.contracts.brief import Assertion, AssertionIn, BriefOp, BriefPatch, ReadinessIn
from analystos.contracts.work import WorkOrderSpec
from analystos.core.errors import PreconditionFailed, ReadinessBlocked, UnsupportedCapability
from analystos.db.base import session_scope
from analystos.db.models import SourceAsset, SourceColumn, WorkOrder
from analystos.services import brief as brief_svc
from analystos.services import readiness
from analystos.services import work_orders as wo_svc


def _refresh(u):
    with session_scope() as s:
        return brief_svc.refresh(s, s.merge(u), WS)


def _patch(u, ops, expected):
    with session_scope() as s:
        return brief_svc.patch(s, s.merge(u), WS, BriefPatch(ops=ops), expected)


def _by_key(doc):
    return {a["key"]: a for a in doc["assertions"]}


def _assess(u, **kw):
    with session_scope() as s:
        return readiness.assess(s, s.merge(u), WS, ReadinessIn(**kw))


def _check(out, name):
    return next(c for c in out["checks"] if c["check"] == name)


def test_suggestions_carry_origin_and_evidence_and_stay_suggestions_until_reviewed(world):  # noqa: F811
    doc = _refresh(world["analyst"])
    assert doc["version"] == 1
    a = _by_key(doc)
    grain = a["data_semantics.grain:sales.orders"]
    assert grain["origin"] == "rule" and grain["review_state"] == "suggested" and grain["evidence"][0]["ref"] == "sales.orders"
    key = a["data_semantics.entity_key:sales.orders"]
    assert key["origin"] == "source" and key["value"] == ["id"]
    assert key["review_state"] == "validated"  # 100 distinct ids in 100 rows, no nulls: validated by the check
    assert any(e["kind"] == "check" and e["detail"]["state"] == "unique" for e in key["evidence"])
    assert a["time_measures.event_time:sales.orders"]["value"] == "ordered_at"
    assert _refresh(world["analyst"])["new_version"] is False  # nothing changed: no new version


def test_review_bumps_versions_and_a_refresh_never_overwrites_a_person(world):  # noqa: F811
    v1 = _refresh(world["analyst"])
    key = "data_semantics.grain:sales.orders"
    with pytest.raises(PreconditionFailed):
        _patch(world["owner"], [BriefOp(op="review", key=key)], expected=7)
    v2 = _patch(world["owner"], [BriefOp(op="review", key=key)], expected=v1["version"])
    g = _by_key(v2)[key]
    assert v2["version"] == 2 and g["review_state"] == "reviewed" and g["version"] == 2 and g["updated_by"] == "user:usr_owner"
    v3 = _patch(world["owner"], [BriefOp(op="set", assertion=AssertionIn(group="data_semantics", field="grain",
                                                                          subject="sales.order_line", value="one row per shipment"))],
                expected=2)
    mine = _by_key(v3)["data_semantics.grain:sales.order_line"]
    assert mine["origin"] == "user" and mine["review_state"] == "reviewed" and mine["version"] == 2
    with session_scope() as s:  # the crawler now infers something else: the person's statement stands
        s.get(SourceAsset, "ast_lines").semantics = {"grain": "one row per order line item", "confidence": 0.9}
        s.get(SourceAsset, "ast_orders").semantics = {"grain": "one row per customer", "confidence": 0.9}
    after = _by_key(_refresh(world["analyst"]))
    assert after["data_semantics.grain:sales.order_line"]["value"] == "one row per shipment"
    assert after["data_semantics.grain:sales.orders"]["value"] == "one row per order"
    with session_scope() as s:
        old = brief_svc.get(s, s.merge(world["viewer"]), WS, version=1)
    assert _by_key(old)[key]["review_state"] == "suggested"  # older versions stay readable


def test_an_unfamiliar_schema_gets_low_confidence_suggestions_and_is_not_ready(world):  # noqa: F811
    with session_scope() as s:
        s.add(SourceAsset(id="ast_t9", source_id="src_sales", workspace_id=WS, schema_name="sales", name="t_x9", source_name="t_x9",
                          kind="table", selected=True, row_count=50, semantics={"grain": "one row per t x9", "confidence": 0.35}))
        s.flush()
        s.add(SourceColumn(asset_id="ast_t9", name="c1", ordinal=0, data_type="text", is_key=False, tags=[], profile={}))
    t9 = _by_key(_refresh(world["analyst"]))["data_semantics.grain:sales.t_x9"]
    assert t9["review_state"] == "suggested" and t9["confidence"] == 0.35
    out = _assess(world["analyst"], job_kind="explain", assets=["sales.t_x9"])
    assert out["status"] == "needs_input"
    g = _check(out, "grain")
    assert g["status"] == "needs_input" and "awaits review" in g["reason"] and g["remediation"]
    assert _check(out, "key_uniqueness")["status"] == "needs_input"


def test_explain_is_ready_once_grain_and_key_are_reviewed(world):  # noqa: F811
    v = _refresh(world["analyst"])
    _patch(world["owner"], [BriefOp(op="review", key="data_semantics.grain:sales.orders")], expected=v["version"])
    out = _assess(world["analyst"], job_kind="explain", assets=["sales.orders"], measures=["amount"])
    assert out["status"] == "ready", out["checks"]
    assert all(c["status"] in ("pass", "not_applicable", "warn") for c in out["checks"] if c["required"])
    assert out["id"].startswith("rdy") and out["brief_version"] == 2


def test_a_failed_required_check_blocks_whatever_else_passes(world):  # noqa: F811
    v = _refresh(world["analyst"])
    _patch(world["owner"], [BriefOp(op="review", key="data_semantics.grain:sales.orders"),
                            BriefOp(op="set", assertion=AssertionIn(group="data_semantics", field="entity_key",
                                                                    subject="sales.orders", value=["state"]))],
           expected=v["version"])
    out = _assess(world["analyst"], job_kind="explain", assets=["sales.orders"])
    assert out["status"] == "blocked"
    key = _check(out, "key_uniqueness")
    assert key["status"] == "fail" and "not unique" in key["reason"]
    assert "score" not in out  # no averaged score exists to hide it


def test_prediction_without_a_label_is_blocked_and_unsupported_here(world):  # noqa: F811
    out = _assess(world["analyst"], job_kind="predict", assets=["sales.orders"])
    assert out["status"] == "unsupported"  # no ML executor on this platform yet
    assert _check(out, "capability")["status"] == "unsupported"
    assert _check(out, "label_availability")["status"] == "fail"
    assert out["alternatives"] == [{"job_kind": "explain", "requires_explicit_choice": True,
                                    "note": out["alternatives"][0]["note"]}]
    with session_scope() as s:
        s.scalar(select(SourceColumn).where(SourceColumn.name == "late")).profile = {"null_rate": 1.0}
    never = _check(_assess(world["analyst"], job_kind="predict", target="sales.orders.late"), "label_availability")
    assert never["status"] == "fail" and "never observed" in never["reason"]


def _work_order(u, spec: dict) -> str:
    with session_scope() as s:
        wo = wo_svc.create(s, s.merge(u), WS, WorkOrderSpec.model_validate(spec))
        return wo.id


def test_unsupported_and_blocked_work_orders_cannot_start(world):  # noqa: F811
    analysis = {"method": "rate_by_segment", "asset": "sales.orders", "outcome": {"type": "is_true", "column": "late"},
                "segment": {"type": "column", "column": "state"}}
    # "predict" with an analysis payload would be a misleading explanation: refused, not run as one
    wo = _work_order(world["analyst"], {"kind": "predict", "objective": "Predict which orders will be late",
                                        "spec": {"type": "analysis", "analyses": [analysis]}})
    with pytest.raises(UnsupportedCapability) as exc:
        wo_svc.start(world["analyst"], WS, wo, expected_revision=1)
    assert exc.value.details["alternatives"][0]["requires_explicit_choice"] is True
    v = _refresh(world["analyst"])
    _patch(world["owner"], [BriefOp(op="set", assertion=AssertionIn(group="data_semantics", field="entity_key",
                                                                    subject="sales.orders", value=["state"]))],
           expected=v["version"])
    wo2 = _work_order(world["analyst"], {"kind": "diagnose", "objective": "Why are orders late by state?",
                                         "spec": {"type": "analysis", "analyses": [analysis]}})
    with pytest.raises(ReadinessBlocked) as blocked:
        wo_svc.start(world["analyst"], WS, wo2, expected_revision=1)
    assert blocked.value.code == "readiness_blocked" and blocked.value.details["checks"][0]["check"] == "key_uniqueness"
    with session_scope() as s:
        assert readiness.latest_for(s, wo2).status == "blocked"
        assert s.get(WorkOrder, wo2).status == "draft"


def test_start_work_lists_every_job_kind_with_its_reasons(world):  # noqa: F811
    from analystos.capabilities import job_kinds

    with session_scope() as s:
        analyst = {k["key"]: k for k in job_kinds.availability(s, s.merge(world["analyst"]), WS)}
        viewer = {k["key"]: k for k in job_kinds.availability(s, s.merge(world["viewer"]), WS)}
    assert list(analyst) == ["explain", "compare", "forecast", "predict", "prepare", "monitor"]
    assert analyst["explain"]["available"] and analyst["compare"]["available"]
    for k in ("forecast", "predict"):
        assert not analyst[k]["available"] and {r["code"] for r in analyst[k]["reasons"]} >= {"no_executor"}
    assert [r["code"] for r in analyst["prepare"]["reasons"]] == ["role"]
    assert not viewer["explain"]["available"] and viewer["explain"]["reasons"][0]["code"] == "role"
    assert analyst["explain"]["entry"]["route"] == f"/api/workspaces/{WS}/work-orders"


def test_memory_items_outside_the_callers_scope_are_withheld():
    from analystos.contracts.policy import DataScope

    scope = DataScope(workspace_id=WS, user_id="u", role="analyst", assets=["sales.orders"],
                      denied_columns=["sales.orders.email"])
    visible = {"mapped_columns": ["sales.orders.amount"]}
    other_table = {"mapped_columns": ["hr.salaries.amount"]}
    denied = {"mapped_columns": ["sales.orders.email"]}
    workspace_note = {"mapped_columns": []}
    assert brief_svc.in_scope(visible, scope) and brief_svc.in_scope(workspace_note, scope)
    assert not brief_svc.in_scope(other_table, scope) and not brief_svc.in_scope(denied, scope)
    assert not brief_svc.in_scope({"subject": "hr.salaries"}, scope)


def test_assertions_refuse_fields_outside_their_group():
    with pytest.raises(ValueError):
        Assertion(key="x", group="constraints", field="pii_access", value="allowed", origin="model")
