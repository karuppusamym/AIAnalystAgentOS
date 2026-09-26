"""P6-04: the recipe IR validator, the SQL/DuckDB compilers, the planner, join pre-flight and the dbt
emitter; P6-05 gate evaluation and schema policy; P6-07 executed column lineage from the IR.

No services: the DuckDB engine runs in memory, and the SQL forms are checked against the gateway
validator. The equality of the SQL (Postgres, through the gateway) and DuckDB results on the same
fixture is `tests/integration/test_recipes.py`."""
from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import pytest
from tests.unit.recipe_fixtures import EXPECTED, RECIPE, SCHEMA, TABLES, recipe, scope_for

from analystos.contracts.recipe import Recipe, RecipeInvalid, validate_recipe
from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.gateway.validator import validate_sql
from analystos.recipes.compiler import Compiler, compile_outputs
from analystos.recipes.execute import (
    ExecutionPlan,
    RecipeExecutor,
    SnapshotStore,
    judge_join,
    plan_execution,
    run_snapshot_job,
)
from analystos.recipes.gates import evaluate, row_gates, schema_policy


def _problems(spec) -> list[str]:
    with pytest.raises(RecipeInvalid) as err:
        validate_recipe(spec)
    return err.value.problems


def _node(spec, node_id):
    return next(n for n in spec["nodes"] if n["id"] == node_id)


# ------------------------------------------------------------------------------------ IR validation
def test_the_fixture_recipe_validates_and_every_node_carries_a_schema():
    v = validate_recipe(RECIPE)
    stamped = v.stamped()
    assert all(n["schema"] for n in stamped["nodes"])
    assert [c.name for c in v.schemas["with_customer"]][-3:] == ["customer_name", "region", "signup_date"]
    assert v.columns("valued")["amount_eur"] == "double"
    assert v.hash == validate_recipe(stamped).hash != Recipe.model_validate(RECIPE).hash()  # stamped or not: one recipe
    assert v.ancestors("out")[0] == "orders" and "legacy" in v.ancestors("out")


@pytest.mark.parametrize("node_id, field, value, expected", [
    ("paid", "predicate", "state = 'paid'", "column state does not exist"),
    ("valued", "columns", [{"name": "x", "expr": "discount * 2", "type": "double"}], "column discount does not exist"),
    ("unique_orders", "keys", ["order_key"], "column order_key does not exist"),
    ("renamed_customers", "mapping", {"nickname": "nick"}, "column nickname does not exist"),
])
def test_an_undeclared_column_reference_is_rejected(node_id, field, value, expected):
    spec = recipe()
    _node(spec, node_id)[field] = value
    assert any(expected in p for p in _problems(spec))


def test_a_gate_on_an_undeclared_column_is_rejected():
    spec = recipe()
    _node(spec, "out")["gates"].append({"type": "not_null", "column": "email"})
    assert any("column email does not exist" in p for p in _problems(spec))


@pytest.mark.parametrize("predicate, expected", [
    ("status = 1", "compares text with numeric"),
    ("order_date >= '2024-01-01'", "compares temporal with text"),
    ("amount + status > 0", "mixes numeric and text"),
    ("status || amount = 'x'", "concatenates a numeric value"),
])
def test_an_implicit_type_change_in_a_predicate_is_rejected(predicate, expected):
    spec = recipe()
    _node(spec, "paid")["predicate"] = predicate
    assert any(expected in p for p in _problems(spec))


def test_a_derived_type_must_match_its_expression_or_cast_explicitly():
    spec = recipe()
    _node(spec, "valued")["columns"][0] = {"name": "amount_eur", "expr": "amount * 0.9", "type": "double"}
    problems = _problems(spec)
    assert any("is numeric(10,2) but declared double" in p and "CAST" in p for p in problems)
    _node(spec, "valued")["columns"][0]["expr"] = "CAST(amount * 0.9 AS DOUBLE)"
    validate_recipe(spec)


def test_output_union_and_join_type_changes_are_rejected():
    spec = recipe()
    _node(spec, "out")["schema"][2] = {"name": "orders", "type": "bigint"}  # the cast node made it integer
    assert any("orders is integer but the output declares bigint" in p for p in _problems(spec))
    spec = recipe()
    _node(spec, "legacy")["schema"][3] = {"name": "amount", "type": "double"}
    assert any("union inputs must match exactly" in p for p in _problems(spec))
    spec = recipe()
    _node(spec, "customers")["schema"][0] = {"name": "customer_id", "type": "text"}
    assert any("are different types" in p for p in _problems(spec))


@pytest.mark.parametrize("expr, expected", [
    ("my_udf(amount)", "unknown function"),
    ("SUM(amount)", "not allowed in an expression"),
    ("(SELECT 1)", "not allowed in an expression"),
    ("o.amount", "must be unqualified"),
])
def test_expressions_are_scalar_known_and_unqualified(expr, expected):
    spec = recipe()
    _node(spec, "valued")["columns"][0] = {"name": "amount_eur", "expr": expr, "type": "double"}
    assert any(expected in p for p in _problems(spec))


def test_dag_shape_is_checked():
    spec = recipe()
    spec["nodes"].insert(0, spec["nodes"].pop(3))  # the union before its inputs
    assert any("must come before it" in p for p in _problems(spec))
    spec = recipe()
    spec["nodes"].insert(3, {"op": "select", "id": "dangling", "input": "orders", "columns": ["order_id"]})
    assert any("dangling" in p and "not read by any node" in p for p in _problems(spec))
    spec = recipe()
    _node(spec, "out")["gates"].append({"type": "row_count_delta", "max_change_pct": 10, "severity": "drop"})
    assert any("only row gates" in p for p in _problems(spec))


def test_a_declared_intermediate_schema_is_checked():
    spec = recipe()
    _node(spec, "renamed_customers")["schema"] = [{"name": "customer_id", "type": "integer"}]
    assert any("declared schema" in p for p in _problems(spec))


# ------------------------------------------------------------------------------------ compilers
def _store(tmp_path) -> tuple[SnapshotStore, dict[str, str]]:
    store = SnapshotStore(tmp_path)
    return store, {a: store.put(t["columns"], t["rows"]) for a, t in TABLES.items()}


def _as_expected(columns, rows) -> dict:
    out = {}
    for r in rows:
        rec = dict(zip(columns, r, strict=False))
        out[rec["customer_id"]] = [rec["region"], rec["orders"], rec["revenue_eur"], rec["best_rank"]]
    return out


def _close(a: dict, b: dict) -> bool:
    if a.keys() != b.keys():
        return False
    for k in a:
        for x, y in zip(a[k], b[k], strict=True):
            if isinstance(x, float) or isinstance(y, float):
                if not math.isclose(float(x), float(y), rel_tol=1e-9):
                    return False
            elif x != y:
                return False
    return True


def test_duckdb_compiler_gives_the_hand_computed_result(tmp_path):
    v = validate_recipe(RECIPE)
    store, tables = _store(tmp_path)
    c = Compiler(v, "duckdb")
    out = run_snapshot_job({"sql": c.sql(c.output_query("out")), "tables": tables, "max_rows": 100,
                            "timeout_seconds": 10}, store=store)
    columns, rows = store.get(out["result"])
    assert columns == ["customer_id", "region", "orders", "revenue_eur", "best_rank"]
    assert _close(_as_expected(columns, rows), EXPECTED)


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb"])
def test_every_compiled_statement_passes_the_gateway_validator(dialect):
    v = validate_recipe(RECIPE)
    scope = scope_for(dialect=dialect)
    c = Compiler(v, dialect)
    gates = row_gates(v.nodes["out"])
    for tree in (c.output_query("out"), c.output_query("out", gates=gates), c.preflight_query("with_customer")):
        validated = validate_sql(scope, c.sql(tree), max_rows=100)
        assert validated.referenced_assets and set(validated.referenced_assets) <= set(TABLES)


def test_compiled_sql_is_built_from_trees_and_quotes_identifiers():
    spec = recipe()
    _node(spec, "customers")["schema"][1] = {"name": "name", "type": "text"}
    sql = compile_outputs(validate_recipe(spec), "postgres")["customer_revenue"]
    assert '"src_fx"."orders" AS "orders"' in sql and 'CAST("orders"."amount" AS DECIMAL(10, 2))' in sql
    assert "DESC NULLS LAST" in sql  # explicit null order, the same on every engine


def test_snapshot_store_is_content_addressed_and_refuses_a_modified_file(tmp_path):
    store = SnapshotStore(tmp_path)
    a = store.put(["x"], [[2], [1]])
    assert store.put(["x"], [[1], [2]]) == a  # row order is not content
    path = store.path(a)
    import gzip
    import json

    body = json.loads(gzip.decompress(path.read_bytes()))
    body["rows"][0] = [99]
    path.write_bytes(gzip.compress(json.dumps(body).encode()))
    with pytest.raises(Conflict):
        store.get(a)


def test_the_snapshot_job_runs_on_the_compute_queue(tmp_path, monkeypatch):
    from analystos.core.config import get_settings
    from analystos.workflows import orchestrator
    from analystos.workflows.activities import BY_WORKLOAD, run_recipe_snapshot
    from analystos.workflows.analysis_workflow import RecipeComputeWorkflow
    from analystos.workflows.queues import DEFAULT_SPECS

    assert run_recipe_snapshot in BY_WORKLOAD["compute"] and DEFAULT_SPECS["compute"].executor == "process"
    assert RecipeComputeWorkflow.__temporal_workflow_definition.name == "RecipeComputeWorkflow"
    monkeypatch.setattr(get_settings(), "orchestrator", "local")
    store, tables = _store(tmp_path / "recipe_snapshots")
    c = Compiler(validate_recipe(RECIPE), "duckdb")
    job = {"sql": c.sql(c.output_query("out")), "tables": tables, "max_rows": 100, "timeout_seconds": 10,
           "artifact_dir": str(tmp_path)}
    assert orchestrator.run_recipe_compute(job) == run_snapshot_job(job)  # the local path is the same pure job


# ------------------------------------------------------------------------------------ planner
def test_single_source_recipes_push_down_and_others_fall_back_with_a_reason():
    v = validate_recipe(RECIPE)
    plan = plan_execution(v, scope_for())
    assert (plan.engine, plan.dialect, plan.source_id, plan.reasons) == ("sql", "postgres", "src_fx_1", [])
    scope = scope_for()
    scope.asset_sources[f"{SCHEMA}.customers"] = "src_other"
    scope.source_ids.append("src_other")
    scope.source_dialects["src_other"] = "postgres"
    plan = plan_execution(v, scope)
    assert plan.engine == "duckdb" and "span 2 sources" in plan.reasons[0]
    plan = plan_execution(v, scope_for(dialect="snowflake"))
    assert plan.engine == "duckdb" and "no recipe compiler for the source dialect snowflake" in plan.reasons[0]
    with pytest.raises(InvalidInput):
        plan_execution(v, scope_for(dialect="snowflake"), prefer="sql")
    assert plan_execution(v, scope_for(), prefer="duckdb").reasons == ["the DuckDB snapshot engine was requested"]


def test_the_planner_checks_the_scope():
    v = validate_recipe(RECIPE)
    scope = scope_for()
    scope.assets.remove(f"{SCHEMA}.customers")
    with pytest.raises(Forbidden):
        plan_execution(v, scope)
    scope = scope_for()
    scope.columns[f"{SCHEMA}.orders"] = ["order_id", "customer_id", "order_date", "status"]
    with pytest.raises(InvalidInput, match="no column amount"):
        plan_execution(v, scope)
    scope = scope_for()
    scope.denied_columns.append(f"{SCHEMA}.customers.name")
    with pytest.raises(Forbidden, match="not readable"):
        plan_execution(v, scope)


# ------------------------------------------------------------------------------------ executor + pre-flight
class _FakeGateway:
    """Serves the fixture tables to the snapshot reads (the executor's only source access)."""

    def __init__(self):
        self.settings = SimpleNamespace(query_max_rows=1000, query_timeout_seconds=30)
        self.calls: list[tuple[str, str]] = []

    def run_sql_for(self, scope, *, actor, run_id=None, source_id=None):
        def run(sql, *, purpose, max_rows=None, use_cache=True):
            self.calls.append((purpose, sql))
            asset = next(a for a in TABLES if f'"{a.split(".")[0]}"."{a.split(".")[1]}"' in sql)
            t = TABLES[asset]
            return SimpleNamespace(query_id=f"q{len(self.calls)}", columns=list(t["columns"]), rows=copy.deepcopy(t["rows"]),
                                   row_count=len(t["rows"]), truncated=len(t["rows"]) > (max_rows or 10**9))
        return run


def _executor(tmp_path, spec=RECIPE, **kw):
    v = validate_recipe(spec)
    plan = ExecutionPlan("duckdb", "duckdb", None, {a: "src_fx_1" for a in TABLES}, ["test"])
    gw = _FakeGateway()
    return RecipeExecutor(gw, scope_for(), v, plan, actor="user:u", store=SnapshotStore(tmp_path), **kw), gw


def test_the_snapshot_path_reads_inputs_through_the_gateway_once_each(tmp_path):
    ex, gw = _executor(tmp_path)
    res = ex.query(ex.compiler.output_query("out"), purpose="recipe.preview")
    assert _close(_as_expected(res.columns, res.rows), EXPECTED)
    assert sorted(p for p, _ in gw.calls) == ["recipe.snapshot"] * 3
    ex.query(ex.compiler.output_query("out"), purpose="recipe.preview")
    assert len(gw.calls) == 3  # the snapshot is reused, not re-read
    assert set(ex.snapshots) == set(TABLES)


def test_a_truncated_input_is_refused(tmp_path):
    ex, _ = _executor(tmp_path, max_rows=3)
    with pytest.raises(InvalidInput, match="snapshot limit"):
        ex.take_snapshots()


def test_a_pinned_snapshot_that_changed_is_refused(tmp_path):
    spec = recipe()
    _node(spec, "orders")["snapshot"] = "0" * 64
    ex, _ = _executor(tmp_path, spec)
    with pytest.raises(Conflict, match="changed since the recipe pinned"):
        ex.take_snapshots()


def test_join_preflight_measures_keys_overlap_and_multiplication(tmp_path):
    ex, _ = _executor(tmp_path)
    stats = ex.preflight("with_customer")
    assert stats["ok"] and stats["observed"] == "many_to_one"
    assert (stats["left_rows"], stats["left_keys"], stats["right_keys"]) == (6, 4, 4)
    assert stats["unmatched_left_rows"] == 1 and stats["row_multiplication"] == 1.0
    assert stats["left_overlap_pct"] == 75.0


def test_join_preflight_refuses_a_violated_cardinality(tmp_path):
    spec = recipe()
    _node(spec, "with_customer")["expected_cardinality"] = "one_to_one"
    ex, _ = _executor(tmp_path, spec)
    assert ex.preflight("with_customer")["ok"] is False
    bad = judge_join("j", "many_to_one", {"left_rows": 4, "left_key_rows": 4, "left_keys": 4, "right_rows": 5,
                                          "right_key_rows": 5, "right_keys": 3, "matched_left_rows": 4,
                                          "matched_keys": 3, "joined_rows": 6})
    assert bad["ok"] is False and bad["observed"] == "one_to_many" and bad["row_multiplication"] == 1.5


# ------------------------------------------------------------------------------------ gates (P6-05)
def _candidate(spec, tmp_path):
    ex, _ = _executor(tmp_path, spec)
    gates = row_gates(ex.v.nodes["out"])
    res = ex.query(ex.compiler.output_query("out", gates=gates), purpose="recipe.run")
    return ex.v.nodes["out"], res


def test_gates_pass_warn_and_block(tmp_path):
    out, res = _candidate(RECIPE, tmp_path)
    outcome = evaluate(out, res.columns, res.rows)
    by = {g["gate"]: g for g in outcome.results}
    assert outcome.blocked is False and len(outcome.kept) == 4
    assert by["accepted_values(region)"]["status"] == "passed"  # NULL is not a value; not_null gates own it
    assert by["key_unique(customer_id)"]["status"] == "passed" and by["range(revenue_eur)"]["checked_rows"] == 4
    spec = recipe()
    _node(spec, "out")["gates"] = [{"type": "not_null", "column": "region", "severity": "warn"},
                                   {"type": "range", "column": "revenue_eur", "max": 100, "severity": "fail"}]
    out, res = _candidate(spec, tmp_path / "b")
    outcome = evaluate(out, res.columns, res.rows)
    by = {g["gate"]: g for g in outcome.results}
    assert by["not_null(region)"]["status"] == "warned" and by["not_null(region)"]["failed_rows"] == 1
    assert by["range(revenue_eur)"]["status"] == "failed" and by["range(revenue_eur)"]["failed_rows"] == 2
    assert outcome.blocked is True and outcome.warnings


def test_drop_gates_move_rows_to_quarantine_counted_and_shown(tmp_path):
    spec = recipe()
    _node(spec, "out")["gates"] = [{"type": "not_null", "column": "region", "severity": "drop"}]
    out, res = _candidate(spec, tmp_path)
    outcome = evaluate(out, res.columns, res.rows)
    assert outcome.blocked is False
    assert len(outcome.kept) == 3 and len(outcome.dropped) == 1
    row, why = outcome.dropped[0]
    assert row[0] == 9 and why == ["not_null(region)"]
    entry = next(g for g in outcome.results if g["gate"] == "not_null(region)")
    assert entry["status"] == "dropped" and entry["failed_rows"] == 1 and entry["sample"][0]["customer_id"] == 9
    assert outcome.summary()["dropped_rows"] == 1


def test_row_count_delta_compares_with_the_last_good_output(tmp_path):
    spec = recipe()
    _node(spec, "out")["gates"] = [{"type": "row_count_delta", "max_change_pct": 10, "severity": "fail"}]
    out, res = _candidate(spec, tmp_path)
    assert evaluate(out, res.columns, res.rows).blocked is False  # no previous output
    assert evaluate(out, res.columns, res.rows, previous_rows=4).blocked is False
    blocked = evaluate(out, res.columns, res.rows, previous_rows=8)
    assert blocked.blocked is True and blocked.results[-1]["change_pct"] == 50.0


def test_schema_policy_evolve_warn_strict():
    v = validate_recipe(RECIPE)
    declared = v.nodes["out"].output_schema
    previous = [{"name": "customer_id", "type": "integer"}, {"name": "region", "type": "text"}]
    assert schema_policy("strict", declared, [c.model_dump() for c in declared])["action"] == "none"
    for policy, action in (("evolve", "accept"), ("warn", "warn"), ("strict", "block")):
        got = schema_policy(policy, declared, previous)
        assert got["action"] == action
        assert {c["column"] for c in got["changes"] if c["change"] == "column_added"} == {"orders", "revenue_eur",
                                                                                          "best_rank"}
    drift = schema_policy("strict", declared, None, [{"change": "type_changed", "column": "amount", "from": "numeric(10,2)",
                                                     "to": "double", "asset": f"{SCHEMA}.orders"}])
    assert drift["action"] == "block" and drift["changes"][0]["upstream"] is True


# ------------------------------------------------------------------------------------ lineage (P6-07)
def test_executed_column_lineage_from_the_ir():
    from analystos.recipes.lineage import column_lineage

    lineage = column_lineage(validate_recipe(RECIPE))[0]
    cols = lineage["columns"]
    assert {(r["asset"], r["column"], r["subtype"]) for r in cols["revenue_eur"]} == {
        (f"{SCHEMA}.orders", "amount", "AGGREGATION"), (f"{SCHEMA}.legacy_orders", "amount", "AGGREGATION")}
    assert {(r["asset"], r["column"]) for r in cols["region"]} == {(f"{SCHEMA}.customers", "region")}
    assert {r["subtype"] for r in cols["customer_id"]} == {"IDENTITY"}
    assert {(r["asset"], r["column"]) for r in cols["orders"]} == set()  # COUNT(*) reads no column
    indirect = {(r["asset"], r["column"]): r["subtypes"] for r in lineage["dataset"]}
    assert "FILTER" in indirect[(f"{SCHEMA}.orders", "status")]
    assert "JOIN" in indirect[(f"{SCHEMA}.customers", "customer_id")]
    assert "SORT" in indirect[(f"{SCHEMA}.legacy_orders", "order_date")]


def test_recipe_openlineage_events_validate_against_the_pinned_schemas():
    from datetime import UTC, datetime

    from analystos.evidence.lineage.sql import redact_literals
    from analystos.evidence.schemas import validate_openlineage
    from analystos.recipes.lineage import column_lineage, openlineage_events

    v = validate_recipe(RECIPE)
    sql = {k: redact_literals(s) for k, s in compile_outputs(v, "postgres").items()}
    now = datetime(2026, 9, 26, tzinfo=UTC)
    events = openlineage_events(workspace_id="ws", recipe_name="customer_orders", recipe_version=1, recipe_run_id="rr_1",
                                lineage=column_lineage(v), sources={a: "src_fx_1" for a in TABLES},
                                outputs={"customer_revenue": {"namespace": "analystos://source/src_out",
                                                              "name": "src_out.customer_orders__customer_revenue",
                                                              "columns": [c.model_dump() for c in v.nodes["out"].output_schema]}},
                                sql=sql, dialect="postgres", started_at=now, finished_at=now, status="succeeded")
    assert [e["eventType"] for e in events] == ["START", "COMPLETE"]
    for e in events:
        assert validate_openlineage(e) == []
    facet = events[1]["outputs"][0]["facets"]["columnLineage"]
    assert facet["fields"]["revenue_eur"]["inputFields"][0]["transformations"][0]["subtype"] == "AGGREGATION"
    assert "'paid'" not in events[1]["job"]["facets"]["sql"]["query"]  # literals redacted before storage


# ------------------------------------------------------------------------------------ dbt emitter
def test_the_emitted_dbt_project_validates_against_the_build_guard():
    from analystos.build.project import check_files
    from analystos.recipes.dbt import emit_project

    project = emit_project(validate_recipe(RECIPE))
    files = project["files"]
    assert set(files) == {"dbt_project.yml", "models/customer_revenue.sql", "models/schema.yml", "models/sources.yml"}
    model = files["models/customer_revenue.sql"]
    assert "{{ source('src_fx', 'orders') }}" in model and "src_fx.orders" not in model.replace("{{ source('src_fx', 'orders') }}", "")
    assert "severity: warn" in files["models/schema.yml"] and "accepted_values" in files["models/schema.yml"]
    check_files(files, allowed_sources=set(TABLES))  # the BuildGateway's static guard
    from analystos.core.errors import PolicyDenied

    with pytest.raises(PolicyDenied):
        check_files(files, allowed_sources={f"{SCHEMA}.orders"})
    assert project["hash"] == emit_project(validate_recipe(RECIPE))["hash"]  # deterministic


def test_the_emitted_dbt_project_parses_with_dbt_core(tmp_path):
    from analystos.build.project import check_manifest
    from analystos.build.runner import DbtCoreRunner, write_profile, write_project
    from analystos.core.config import get_settings
    from analystos.recipes.dbt import emit_project

    runner = DbtCoreRunner(getattr(get_settings(), "dbt_executable", "dbt") or "dbt")
    if not runner.available():
        pytest.skip("dbt Core is not installed (ANALYSTOS_DBT_EXECUTABLE)")
    project = emit_project(validate_recipe(RECIPE))
    write_project(tmp_path / "project", project["files"])
    write_profile(tmp_path / "profile", None, schema="aos_mart")
    result = runner.parse(tmp_path)
    assert result.ok, result.log_tail
    check_manifest(result.manifest, project=project["name"], target_schema="aos_mart", allowed_sources=set(TABLES))
