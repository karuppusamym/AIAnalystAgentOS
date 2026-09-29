"""N-9 what-if scenarios: a governed SemanticQuery with declared changes. The observed baseline is the
compiled query's result (real compiler, DuckDB standing in for the gateway); the scenario is deterministic
arithmetic; every number is labelled observed or simulated; the numbers guard and the publish contract
never let a simulated number pass as observed; assumptions are recorded and hashed."""
from __future__ import annotations

import duckdb
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.contracts.bi import ChartSpec, PublishBundle
from analystos.contracts.policy import DataScope
from analystos.contracts.scenario import LabelledValue, ScenarioCell, ScenarioSpec
from analystos.contracts.semantic import SemanticQuery
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, stable_hash
from analystos.db.base import session_scope
from analystos.db.models import AuditEvent, LineageEdge, RunEvent, WhatIfScenario
from analystos.gateway.types import QueryResult
from analystos.semantic.compiler import compile_query
from analystos.services import scenarios as svc
from analystos.skills import result_facts
from analystos.skills import scenario as skill

CATALOG = {"id": "model1", "version": 3, "hash": "modelhash", "datasets": [
    {"name": "orders", "source": "SELECT amount, region, priority, paid FROM sales.orders", "fields": [
        {"name": n, "expressions": [{"expression": n}], "dimension": {} if n in ("region", "priority") else None}
        for n in ["amount", "region", "priority", "paid"]]}],
    "metrics": {
        "revenue": {"id": "m1", "version": 2, "hash": "h1", "definition": {
            "name": "revenue", "display_name": "Paid revenue", "dataset": "orders", "expressions": [{"expression": "SUM(amount)"}],
            "dimensions": ["region", "priority"], "filters": ["paid = TRUE"]}},
        "avg_order": {"id": "m2", "version": 1, "hash": "h2", "definition": {
            "name": "avg_order", "display_name": "Average order", "dataset": "orders", "expressions": [{"expression": "AVG(amount)"}],
            "dimensions": ["region", "priority"], "filters": ["paid = TRUE"]}}}}
ROWS = [(100, "East", 1, True), (300, "East", 3, True), (50, "East", 2, False), (200, "West", 1, True), (40, "West", 4, True)]


class DuckRuntime:
    """The real compiler over an approved catalog; DuckDB answers what the gateway would."""

    def __init__(self):
        self.catalog = CATALOG
        self.scope = DataScope(workspace_id=WS, user_id="u", role="analyst", source_ids=["s"], assets=["sales.orders"],
                               asset_sources={"sales.orders": "s"}, columns={"sales.orders": ["amount", "region", "priority", "paid"]},
                               source_dialects={"s": "duckdb"})
        self.db = duckdb.connect()
        self.db.execute("CREATE SCHEMA sales")
        self.db.execute("CREATE TABLE sales.orders(amount DOUBLE, region VARCHAR, priority INTEGER, paid BOOLEAN)")
        self.db.executemany("INSERT INTO sales.orders VALUES (?, ?, ?, ?)", [list(r) for r in ROWS])
        self.ran: list[str] = []

    def semantic(self, query, *, step_id):  # noqa: ANN001
        compiled = compile_query(SemanticQuery.model_validate(query), self.catalog, self.scope)
        cur = self.db.execute(compiled.sql)
        rows = [list(r) for r in cur.fetchall()]
        self.ran.append(compiled.sql)
        return compiled.sql, compiled.provenance, QueryResult(
            query_id=new_id("qry"), columns=[d[0] for d in cur.description], rows=rows, row_count=len(rows),
            fingerprint=stable_hash(compiled.sql), result_hash=stable_hash(rows), referenced_assets=["sales.orders"], sql=compiled.sql)


def _spec(**kw) -> ScenarioSpec:
    base = {"name": "East up 10%", "semantic_query": {"metrics": ["revenue", "avg_order"], "dimensions": ["region"]},
            "adjustments": [{"kind": "scale", "metric": "revenue", "percent": 10, "segment": {"dimension": "region", "values": ["East"]}}],
            "assumptions": ["East campaign lifts paid revenue"]}
    return ScenarioSpec.model_validate({**base, **kw})


@pytest.fixture
def scn(world, sqlite_db):  # noqa: F811
    from analystos.db import models

    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables["what_if_scenario"]])
    return world


# ------------------------------------------------------------------------------------ arithmetic and labels
def test_scale_applies_to_the_segment_only_and_labels_every_number():
    rows, totals = skill.apply(_spec(), ["region", "revenue", "avg_order"], [["East", 400.0, 200.0], ["West", 240.0, 120.0]],
                               summable_metrics=["revenue"])
    east, west = ({c.metric: c for c in r.cells} for r in rows)
    assert east["revenue"].observed == LabelledValue(value=400, basis="observed")
    assert east["revenue"].simulated == LabelledValue(value=440, basis="simulated")
    assert (east["revenue"].change.value, east["revenue"].change_pct.value) == (40, 0.1)
    assert east["revenue"].adjusted_by == [0] and west["revenue"].adjusted_by == []
    assert west["revenue"].simulated.value == 240 and west["revenue"].change.value == 0
    assert east["avg_order"].simulated.value == 200  # an unadjusted metric is carried, still labelled simulated
    # Totals only for a summable metric (SUM / COUNT), never for an average.
    assert [(t.metric, t.observed.value, t.simulated.value) for t in totals] == [("revenue", 640, 680)]


def test_shift_set_in_order_and_no_totals_for_truncated_results():
    spec = _spec(adjustments=[{"kind": "shift", "metric": "revenue", "amount": -40},
                              {"kind": "set", "metric": "avg_order", "amount": 150, "segment": {"dimension": "region", "values": ["West"]}}])
    rows, totals = skill.apply(spec, ["region", "revenue", "avg_order"], [["East", 400, 200], ["West", 240, 120]],
                               summable_metrics=["revenue"], truncated=True)
    assert [c.simulated.value for c in rows[0].cells] == [360, 200]
    assert [c.simulated.value for c in rows[1].cells] == [200, 150]
    assert totals == []


def test_summable_reads_the_aggregate():
    assert skill.summable("SUM(amount)") and skill.summable("COUNT(*)")
    assert not any(skill.summable(e) for e in ("AVG(amount)", "COUNT(DISTINCT id)", "SUM(a) / COUNT(*)", "MAX(x)", "SUM("))


def test_a_cell_cannot_carry_the_wrong_label():
    obs, sim = LabelledValue(value=1, basis="observed"), LabelledValue(value=1, basis="simulated")
    with pytest.raises(ValidationError):
        ScenarioCell(metric="m", observed=sim, simulated=sim, change=sim, change_pct=sim)
    with pytest.raises(ValidationError):
        ScenarioCell(metric="m", observed=obs, simulated=obs, change=sim, change_pct=sim)


@pytest.mark.parametrize("change, message", [
    ({"adjustments": [{"kind": "scale", "metric": "margin", "percent": 5}]}, "not a metric"),
    ({"adjustments": [{"kind": "scale", "metric": "revenue", "percent": 5, "segment": {"dimension": "priority", "values": [1]}}]},
     "not grouped"),
    ({"adjustments": [], "filter_overrides": [{"field": "priority", "value": 2}]}, "exactly one filter"),
])
def test_changes_must_name_what_the_query_returns(change, message):
    with pytest.raises(InvalidInput, match=message):
        skill.validate(_spec(**change))


@pytest.mark.parametrize("body", [
    {"adjustments": []},
    {"adjustments": [{"kind": "scale", "metric": "revenue"}]},
    {"adjustments": [{"kind": "set", "metric": "revenue", "percent": 3}]},
    {"adjustments": [{"kind": "scale", "metric": "revenue", "percent": -150}]},
    {"assumptions": ["  "]},
])
def test_spec_shape_is_refused_before_anything_runs(body):
    with pytest.raises(ValidationError):
        _spec(**body)


def test_threshold_change_is_the_same_query_with_the_new_filter_value():
    spec = _spec(semantic_query={"metrics": ["revenue"], "filters": [{"field": "priority", "op": "<=", "value": 2}]},
                 adjustments=[], filter_overrides=[{"field": "priority", "value": 3}])
    alt = skill.remeasure_query(spec)
    assert alt.filters[0].op == "<=" and alt.filters[0].value == 3
    assert spec.semantic_query.filters[0].value == 2
    with pytest.raises(InvalidInput):
        skill.remeasure_query(spec.model_copy(update={"filter_overrides": [
            spec.filter_overrides[0].model_copy(update={"op": "in", "value": 3})]}))


# ------------------------------------------------------------------------------------ numbers guard
def test_guard_needs_simulated_numbers_labelled_and_refuses_them_where_observed():
    observed, simulated = [400.0], [440.0, 40.0, 0.1]
    assert skill.guard("Observed revenue: 400. Simulated revenue under this scenario: 440 (+40, +10.0%).", observed, simulated)["ok"]
    unlabelled = skill.guard("Revenue is 440.", observed, simulated)
    assert not unlabelled["ok"] and "without saying it is simulated" in unlabelled["problems"][0]
    assert not skill.guard("Observed revenue is 440.", observed, simulated)["ok"]
    assert "matches no observed or simulated value" in skill.guard("Simulated revenue would be 999.", observed, simulated)["problems"][0]
    # An observed context (a report, a published chart, a finding): a simulated number is refused, label or not.
    published = skill.guard("Simulated revenue would be 440.", observed, simulated, allow_simulated=False)
    assert not published["ok"] and "cannot be presented as observed" in published["problems"][0]
    assert skill.guard("Revenue is 400.", observed, simulated, allow_simulated=False)["ok"]
    # The existing observed-facts guard (Ask synthesis, REV) never binds a simulated value either.
    assert not result_facts.numbers_bound("Revenue is 440 (step 1).", observed)["ok"]


def test_a_simulated_chart_cannot_be_published_and_observed_charts_hash_as_before():
    chart = {"key": "c1", "title": "t", "chart_type": "bar", "intent": "comparison", "dataset": "d"}
    assert "value_basis" not in ChartSpec.model_validate(chart).model_dump()
    bundle = {"workspace_id": WS, "datasets": [], "metrics": [], "dashboards": []}
    PublishBundle.model_validate({**bundle, "charts": [chart]})
    with pytest.raises(ValidationError, match="simulated"):
        PublishBundle.model_validate({**bundle, "charts": [{**chart, "value_basis": "simulated"}]})


# ------------------------------------------------------------------------------------ service
def test_service_observes_through_the_compiler_simulates_records_and_hashes(scn):
    rt = DuckRuntime()
    out = svc.run(scn["analyst"], WS, _spec(), runtime=rt)
    assert len(rt.ran) == 1 and out["label"] == "simulated" and out["publishable"] is False
    assert out["baseline"]["basis"] == "observed" and out["baseline"]["semantic"]["compiler_version"]
    east = next(r for r in out["rows"] if r["key"]["region"] == "East")
    rev = next(c for c in east["cells"] if c["metric"] == "revenue")
    assert rev["observed"] == {"value": 400, "basis": "observed"} and rev["simulated"] == {"value": 440, "basis": "simulated"}
    assert out["totals"][0]["simulated"]["value"] == 680 and len(out["totals"]) == 1
    assert out["guard"]["ok"] and "Simulated Paid revenue" in out["summary"] and "Observed Paid revenue" in out["summary"]
    assert out["assumptions"][0] == "East campaign lifts paid revenue"
    assert "Paid revenue changes by +10% where region is East." in out["assumptions"]
    assert out["assumptions"][-1] == skill.CAVEAT and out["assumptions_hash"] == stable_hash(out["assumptions"])
    again = svc.run(scn["analyst"], WS, _spec(), runtime=DuckRuntime())
    assert again["id"] != out["id"]
    assert (again["spec_hash"], again["assumptions_hash"], again["result_hash"]) == (out["spec_hash"], out["assumptions_hash"], out["result_hash"])
    changed = svc.run(scn["analyst"], WS, _spec(assumptions=["A different reason"]), runtime=DuckRuntime())
    assert changed["assumptions_hash"] != out["assumptions_hash"] and changed["result_hash"] != out["result_hash"]
    with session_scope() as s:
        row = s.get(WhatIfScenario, out["id"])
        assert row.semantic_model_version == 3 and row.scenario_version == "whatif.v1"
        edges = {(e.from_type, e.relation, e.to_type) for e in s.scalars(select(LineageEdge).where(LineageEdge.from_id == out["id"]))}
        assert ("scenario", "derived_from", "query") in edges
        assert s.scalar(select(RunEvent).where(RunEvent.type == "scenario.computed")).payload["label"] == "simulated"
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "scenario.computed", AuditEvent.target == out["id"]))


def test_threshold_change_is_remeasured_through_the_compiler_and_labelled_simulated(scn):
    rt = DuckRuntime()
    spec = _spec(semantic_query={"metrics": ["revenue"], "dimensions": ["region"],
                                 "filters": [{"field": "priority", "op": "<=", "value": 1}]},
                 adjustments=[], filter_overrides=[{"field": "priority", "value": 3}])
    out = svc.run(scn["analyst"], WS, spec, runtime=rt)
    assert len(rt.ran) == 2 and "<= 3" in rt.ran[1] and "<= 1" in rt.ran[0]
    assert out["remeasured"]["basis"] == "simulated"
    cells = {r["key"]["region"]: r["cells"][0] for r in out["rows"]}
    assert cells["East"]["observed"]["value"] == 100 and cells["East"]["simulated"]["value"] == 400
    assert cells["West"]["observed"]["value"] == 200 and cells["West"]["simulated"]["value"] == 200
    assert any("instead of <= 1" in a for a in out["assumptions"])


def test_scenarios_are_private_and_need_a_model(scn):
    out = svc.run(scn["analyst"], WS, _spec(), runtime=DuckRuntime())
    with session_scope() as s:
        assert svc.load(s, s.merge(scn["analyst"]), out["id"], WS).id == out["id"]
        assert [x["id"] for x in svc.list_for(s, s.merge(scn["analyst"]), WS)] == [out["id"]]
        assert svc.list_for(s, s.merge(scn["owner"]), WS) == []
        with pytest.raises(NotFound):
            svc.load(s, s.merge(scn["owner"]), out["id"], WS)
        with pytest.raises(NotFound):
            svc.load(s, s.merge(scn["analyst"]), out["id"], "ws_other")
    rt = DuckRuntime()
    rt.catalog = None
    with pytest.raises(InvalidInput, match="approved semantic model"):
        svc.run(scn["analyst"], WS, _spec(), runtime=rt)
    assert rt.ran == []
