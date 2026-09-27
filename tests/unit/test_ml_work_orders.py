"""ML work orders: a WorkOrderSpec carrying a complete MLSpec starts a `playbook.train` run of the published ml_spec
definition with exactly that content (by content hash); an unpublished or draft spec is refused, an incomplete one
is still not executable, and Start work offers ML once a published ml_spec exists."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.contracts.definition import DefinitionDraftIn
from analystos.contracts.work import WorkOrderSpec
from analystos.core.errors import PolicyDenied, UnsupportedCapability
from analystos.db.base import session_scope
from analystos.db.models import User, WorkOrder
from analystos.services import definitions as defs
from analystos.services import work_orders as wo_svc

ML = {"type": "ml", "task": "classify", "dataset": {"asset": "sales.orders", "source_id": "src_sales"}, "target": "late",
      "entity_keys": ["id"], "features": [{"column": "amount"}, {"column": "state"}], "seed": 7}


@pytest.fixture
def started(world, monkeypatch):  # noqa: F811
    from analystos.services import readiness, runs

    monkeypatch.setattr(readiness, "assess_work_order", lambda s, u, wo: {"status": "ready", "checks": [], "id": "ra_1",
                                                                          "alternatives": []})
    calls: list[dict] = []

    def fake_start(user, workspace_id, **kw):
        calls.append({"workspace_id": workspace_id, **kw})
        return SimpleNamespace(id=f"run_ml{len(calls)}"), False

    monkeypatch.setattr(runs, "start_run_request", fake_start)
    return calls


def _wo(user, spec=ML) -> str:
    with session_scope() as s:
        return wo_svc.create(s, s.merge(user), WS, WorkOrderSpec.model_validate(
            {"kind": "predict", "objective": "Predict which orders will be late", "spec": spec})).id


def _definition(owner, status: str) -> str:
    with session_scope() as s:
        spec = dict(ML)
        row = defs.create_draft(s, s.merge(owner), WS, DefinitionDraftIn(kind="ml_spec", key="late_orders", spec=spec))
        if status == "published":
            row = defs.publish(s, s.merge(owner), row, row.revision)
        return row.id


def test_an_unpublished_ml_spec_is_refused(world, started):  # noqa: F811
    wo = _wo(world["analyst"])
    with pytest.raises(PolicyDenied, match="not a published ml_spec definition"):
        wo_svc.start(world["analyst"], WS, wo, expected_revision=1)
    _definition(world["owner"], "draft")
    with pytest.raises(PolicyDenied, match="which is draft"):
        wo_svc.start(world["analyst"], WS, wo, expected_revision=1)
    assert started == []
    with session_scope() as s:
        assert s.get(WorkOrder, wo).status == "draft"


def test_a_published_ml_spec_starts_a_training_run(world, started):  # noqa: F811
    def_id = _definition(world["owner"], "published")
    wo = _wo(world["analyst"])
    run, replayed = wo_svc.start(world["analyst"], WS, wo, expected_revision=1)
    assert run.id == "run_ml1" and replayed is False
    call = started[0]
    assert call["playbook"] == "playbook.train" and call["source_ids"] == ["src_sales"]
    assert call["origin"]["ml_definition"] == {"id": def_id, "key": "late_orders", "version": 1}
    assert call["origin"]["type"] == "work_order" and call["origin"]["work_order_id"] == wo
    with session_scope() as s:
        row = s.get(WorkOrder, wo)
        assert row.status == "started" and row.run_ids == ["run_ml1"]
    # different content (another seed) is not the published version, even under the same key
    other = _wo(world["analyst"], {**ML, "seed": 8})
    with pytest.raises(PolicyDenied, match="not a published"):
        wo_svc.start(world["analyst"], WS, other, expected_revision=1)


def test_an_incomplete_ml_spec_is_still_not_executable(world, started):  # noqa: F811
    wo = _wo(world["analyst"], {"type": "ml", "task": "forecast", "target_metric": "orders", "horizon": 4})
    with pytest.raises(UnsupportedCapability, match="incomplete"):
        wo_svc.start(world["analyst"], WS, wo, expected_revision=1)


def test_start_work_offers_ml_once_an_ml_spec_is_published(world):  # noqa: F811
    from analystos.capabilities import job_kinds

    def reasons(key):
        with session_scope() as s:
            kinds = {k["key"]: k for k in job_kinds.availability(s, s.get(User, "usr_analyst"), WS)}
        return {r["code"] for r in kinds[key]["reasons"]}

    assert "no_executor" in reasons("predict")
    _definition(world["owner"], "published")
    assert "no_executor" not in reasons("predict")
