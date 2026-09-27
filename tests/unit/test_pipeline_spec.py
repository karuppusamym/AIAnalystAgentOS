"""P6-01/P6-02/P6-03 contracts without services: a PipelineSpec is checked against the recipe IR it wraps;
the incremental block's rules; the watermark window the compiler adds; the managed writer's identity and
destination guards."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from tests.unit.recipe_fixtures import RECIPE, SCHEMA

from analystos.contracts.recipe import Incremental, RecipeInvalid, validate_recipe
from analystos.contracts.work import PipelineSpec, WorkOrderSpec
from analystos.core.errors import Forbidden, InvalidInput
from analystos.pipelines import spec as pipeline_spec
from analystos.pipelines.writer import check_credentials, check_destination, check_identities, version_table
from analystos.recipes.compiler import Compiler, SourceWindow

OUT = next(n for n in RECIPE["nodes"] if n["op"] == "output")


def _pipeline(**changes) -> dict:
    p = {"type": "pipeline", "name": "customer_revenue", "recipes": [{"name": "customer_orders"}],
         "inputs": [{"asset": f"{SCHEMA}.orders", "max_age_hours": 24}],
         "output": {"output": "customer_revenue", "grain": ["customer_id"], "keys": ["customer_id"],
                    "schema": copy.deepcopy(OUT["schema"])},
         "joins": [{"node": "with_customer", "expected_cardinality": "many_to_one", "max_unmatched_pct": 25,
                    "max_row_multiplication": 1.0}],
         "checks": [{"name": "revenue", "func": "sum", "input_node": "valued", "input_column": "amount_eur",
                     "output_column": "revenue_eur", "tolerance_pct": 0.01},
                    {"name": "orders", "func": "count", "input_node": "valued", "output_column": "orders"}],
         "freshness": {"max_age_hours": 26}, "budgets": {"max_queries": 20, "max_rows": 10000},
         "destination": {"schema": "mart", "table": "customer_revenue"}}
    p.update(changes)
    return p


def _check(p: dict, recipe: dict = RECIPE):
    return pipeline_spec.check(pipeline_spec.parse(p), {recipe["name"]: recipe})


def test_a_pipeline_matching_its_recipe_validates_and_fits_a_work_order():
    validated = _check(_pipeline())
    assert set(validated) == {"customer_orders"}
    wo = WorkOrderSpec.model_validate({"kind": "prepare", "objective": "Customer revenue mart for finance",
                                       "spec": _pipeline()})
    assert isinstance(wo.spec, PipelineSpec) and wo.spec.output_recipe() == "customer_orders" and not wo.executable


@pytest.mark.parametrize("change, problem", [
    ({"output": {**_pipeline()["output"], "keys": ["region"]}}, "output.keys"),
    ({"output": {**_pipeline()["output"], "grain": []}}, "output.grain"),
    ({"output": {**_pipeline()["output"], "schema": [{"name": "customer_id", "type": "bigint"}]}}, "output.schema"),
    ({"output": {**_pipeline()["output"], "output": "nope"}}, "has no output nope"),
    ({"joins": [{"node": "with_customer", "expected_cardinality": "one_to_many"}]}, "declared many_to_one in the recipe"),
    ({"joins": [{"node": "valued", "expected_cardinality": "one_to_one"}]}, "valued is not a join"),
    ({"checks": [{"name": "x", "input_node": "valued", "input_column": "status", "output_column": "revenue_eur"}]},
     "sum reconciles numeric columns"),
    ({"checks": [{"name": "x", "input_node": "ghost", "input_column": "a", "output_column": "b"}]}, "node ghost"),
    ({"inputs": [{"asset": "src_fx.elsewhere"}]}, "not read by any recipe"),
    ({"destination": {"schema": "src_fx", "table": "t"}}, "cannot be a destination"),
    ({"recipes": [{"name": "missing"}]}, "missing not found"),
])
def test_contract_violations_are_listed(change, problem):
    with pytest.raises(pipeline_spec.PipelineInvalid) as err:
        _check(_pipeline(**change))
    assert any(problem in p for p in err.value.problems), err.value.problems


def test_spec_shape_is_strict():
    with pytest.raises(pipeline_spec.PipelineInvalid):
        pipeline_spec.parse({**_pipeline(), "recipe": {}})  # the placeholder's free-form field is gone
    with pytest.raises(pipeline_spec.PipelineInvalid):
        pipeline_spec.parse({**_pipeline(), "recipes": []})


def _row_level(**inc) -> dict:
    """orders -> filter -> derive -> join customers (left) -> output keyed by order_id: incremental-safe."""
    nodes = [n for n in copy.deepcopy(RECIPE["nodes"]) if n["id"] in ("orders", "customers", "renamed_customers")]
    nodes += [{"op": "derive", "id": "valued", "input": "orders", "columns": [
                  {"name": "amount_eur", "expr": "CAST(amount * 0.9 AS DOUBLE)", "type": "double"}]},
              {"op": "join", "id": "with_customer", "left": "valued", "right": "renamed_customers", "how": "left",
               "on": [{"left": "customer_id", "right": "customer_id"}], "expected_cardinality": "many_to_one"},
              {"op": "select", "id": "picked", "input": "with_customer",
               "columns": ["order_id", "customer_id", "order_date", "amount_eur", "region"]},
              {"op": "output", "id": "out", "input": "picked", "name": "orders_enriched", "keys": ["order_id"],
               "schema": [{"name": "order_id", "type": "integer"}, {"name": "customer_id", "type": "integer"},
                          {"name": "order_date", "type": "date"}, {"name": "amount_eur", "type": "double"},
                          {"name": "region", "type": "text"}]}]
    spec = {"kind": "Recipe", "name": "orders_enriched", "nodes": nodes}
    if inc:
        spec["incremental"] = inc
    return spec


def test_incremental_rules_of_the_ir():
    ok = validate_recipe(_row_level(watermark="order_date", key=["order_id"], late_window="1d", deletes="soft"))
    assert ok.recipe.incremental.late_window == 86400
    with pytest.raises(RecipeInvalid) as err:  # an aggregate cannot run on a window
        validate_recipe({**RECIPE, "incremental": {"watermark": "order_date", "key": ["customer_id"], "source": "orders"}})
    assert any("computes across rows" in p for p in err.value.problems)
    assert any("cannot de-duplicate against rows outside it" in p or "de-duplicates on" in p for p in err.value.problems)
    for inc, problem in [({"watermark": "ghost", "key": ["order_id"]}, "no source declares the watermark"),
                         ({"watermark": "region", "key": ["order_id"]}, "reads the incremental source on its right side"),
                         ({"watermark": "status", "key": ["order_id"], "source": "orders"}, "must be a timestamp"),
                         ({"watermark": "order_date", "key": ["region_x"]}, "not a column of output"),
                         ({"watermark": "order_date", "key": ["customer_id"]}, "differs from output")]:
        with pytest.raises(RecipeInvalid) as err:
            validate_recipe(_row_level(**inc))
        assert any(problem in p for p in err.value.problems), (inc, err.value.problems)
    # the pipeline applies its block to a recipe without one; a different block is refused
    p = _pipeline(name="orders_enriched", recipes=[{"name": "orders_enriched"}], joins=[], checks=[], destination=None,
                  inputs=[], incremental={"watermark": "order_date", "key": ["order_id"]},
                  output={"output": "orders_enriched", "keys": ["order_id"],
                          "schema": _row_level()["nodes"][-1]["schema"]})
    v = _check(p, _row_level())["orders_enriched"]
    assert v.recipe.incremental == Incremental(watermark="order_date", key=["order_id"])
    with pytest.raises(pipeline_spec.PipelineInvalid, match="differs"):
        _check(p, _row_level(watermark="order_date", key=["order_id"], late_window=60))


def test_the_watermark_window_is_part_of_the_compiled_source():
    v = validate_recipe(_row_level(watermark="order_date", key=["order_id"]))
    win = {"orders": SourceWindow("order_date", "date", "2024-04-02", "2024-04-06")}
    for dialect in ("postgres", "duckdb"):
        sql = Compiler(v, dialect, windows=win).sql(Compiler(v, dialect, windows=win).output_query("out"))
        assert "2024-04-02" in sql and "2024-04-06" in sql and ">=" in sql and "<=" in sql
    full = Compiler(v, "postgres", windows={"orders": SourceWindow("order_date", "date", None, "2024-04-06",
                                                                     keep_nulls=True)})
    sql = full.sql(full.output_query("out"))
    assert "IS NULL" in sql and ">=" not in sql.split("FROM")[1].split(")")[0]
    assert "2024-04" not in Compiler(v, "postgres").sql(Compiler(v, "postgres").output_query("out"))


def _settings(**over):
    base = dict(env="dev", database_url="postgresql+psycopg://analystos:x@db:5432/analystos",
                analytics_reader_url="postgresql+psycopg://r:x@db:5432/analytics",
                analytics_loader_url="postgresql+psycopg://l:x@db:5432/analytics",
                analytics_builder_url="postgresql+psycopg://b:x@db:5432/analytics",
                analytics_writer_url="postgresql+psycopg://w:secret@db:5432/analytics")
    base.update(over)
    return SimpleNamespace(**base)


def test_the_writer_is_its_own_identity_and_never_writes_sources():
    check_identities(_settings())
    for reused in ("r", "l", "b", "analystos"):
        with pytest.raises(InvalidInput, match="dedicated writer identity"):
            check_identities(_settings(analytics_writer_url=f"postgresql+psycopg://{reused}:x@db:5432/analytics"))
    with pytest.raises(InvalidInput, match="control-plane"):
        check_identities(_settings(analytics_writer_url="postgresql+psycopg://w:x@db:5432/analystos"))
    with pytest.raises(InvalidInput, match="development password"):
        check_credentials(_settings(env="prod", analytics_writer_url="postgresql+psycopg://w:writer@db:5432/analytics"))
    check_credentials(_settings(env="prod"))
    for schema in ("src_abc", "public", "pg_catalog", "information_schema", "analytics"):
        with pytest.raises(Forbidden):
            check_destination(schema)
    with pytest.raises(Forbidden):
        check_destination("aos_mart", forbidden={"aos_mart"})  # a build target
    with pytest.raises(InvalidInput):
        check_destination("mart", "orders__v2")
    check_destination("mart", "orders")
    assert version_table("orders", 3) == "orders__v3"
