"""PipelineSpec against its recipes (P6-01): the pipeline is a contract around published recipes, so every
claim it makes is checked against the validated IR before anything runs.

* the output contract (grain, keys, schema) equals the named recipe output's;
* every join expectation names a join of that recipe and repeats its declared cardinality (a recipe edit
  that loosens a join fails the pipeline instead of passing silently);
* every reconciliation names a node and columns that exist, with a numeric column for `sum`;
* every pinned input is a source asset of one of the recipes;
* the incremental block is the recipe's own, or is applied to it and must pass the IR's incremental rules;
* the destination (when given) is a plain schema.table the managed writer may use.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from analystos.contracts.recipe import (
    JoinNode,
    OutputNode,
    RecipeInvalid,
    ValidatedRecipe,
    family,
    validate_recipe,
)
from analystos.contracts.work import PipelineSpec
from analystos.core.errors import InvalidInput


class PipelineInvalid(InvalidInput):
    code = "pipeline_invalid"

    def __init__(self, problems: list[str]) -> None:
        super().__init__("pipeline rejected: " + "; ".join(problems[:10]), details={"problems": problems})
        self.problems = problems


def parse(spec: PipelineSpec | dict[str, Any]) -> PipelineSpec:
    if isinstance(spec, PipelineSpec):
        return spec
    try:
        return PipelineSpec.model_validate(spec)
    except ValidationError as exc:
        raise PipelineInvalid([f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]) from None


def effective_recipe(pipeline: PipelineSpec, name: str, recipe_spec: dict[str, Any]) -> ValidatedRecipe:
    """The recipe as the pipeline runs it: with the pipeline's incremental block when the recipe has none."""
    spec = dict(recipe_spec)
    if pipeline.incremental is not None and name == pipeline.output_recipe():
        given = pipeline.incremental.model_dump(mode="json", exclude_none=True)
        if spec.get("incremental") not in (None, given):
            raise PipelineInvalid([f"incremental: the block differs from recipe {name}'s own; they must be the same"])
        spec["incremental"] = given
    try:
        return validate_recipe(spec)
    except RecipeInvalid as exc:
        raise PipelineInvalid([f"recipe {name}: {p}" for p in exc.problems]) from None


def check(pipeline: PipelineSpec, recipes: dict[str, dict[str, Any]]) -> dict[str, ValidatedRecipe]:
    """Validate the pipeline against `recipes` ({name: stored recipe spec}); returns the validated recipes
    (the output recipe with the effective incremental block) or raises PipelineInvalid listing every problem."""
    problems: list[str] = []
    names = [r.name for r in pipeline.recipes]
    if len(set(names)) != len(names):
        problems.append("recipes: a recipe is listed twice")
    missing = [n for n in names if n not in recipes]
    if missing:
        problems.append(f"recipes: {', '.join(missing)} not found (published, or the pinned version)")
    out_recipe = pipeline.output_recipe()
    if out_recipe not in names:
        problems.append(f"output.recipe: {out_recipe} is not one of the pipeline's recipes")
    if problems:
        raise PipelineInvalid(problems)
    validated: dict[str, ValidatedRecipe] = {}
    for n in names:
        try:
            validated[n] = effective_recipe(pipeline, n, recipes[n])
        except PipelineInvalid as exc:
            problems += exc.problems
    if problems:
        raise PipelineInvalid(problems)
    v = validated[out_recipe]
    out = next((o for o in v.outputs() if o.name == pipeline.output.output), None)
    if out is None:
        raise PipelineInvalid([f"output.output: recipe {out_recipe} has no output {pipeline.output.output} "
                               f"(has: {', '.join(o.name for o in v.outputs())})"])
    _contract(pipeline, out, problems)
    _joins(pipeline, v, problems)
    _reconciliations(pipeline, v, out, problems)
    sources = {s.asset for r in validated.values() for s in r.sources()}
    for i in pipeline.inputs:
        if i.asset not in sources:
            problems.append(f"inputs: {i.asset} is not read by any recipe of the pipeline")
    if pipeline.destination is not None:
        from analystos.pipelines.writer import check_destination

        try:
            check_destination(pipeline.destination.schema_name, pipeline.destination.table)
        except InvalidInput as exc:
            problems.append(f"destination: {exc.message}")
    if problems:
        raise PipelineInvalid(problems)
    return validated


def _contract(pipeline: PipelineSpec, out: OutputNode, problems: list[str]) -> None:
    declared = [(c.name, c.type) for c in pipeline.output.output_schema]
    actual = [(c.name, c.type) for c in out.output_schema or []]
    if declared != actual:
        problems.append(f"output.schema {declared} differs from recipe output {out.name}'s {actual}")
    if list(pipeline.output.grain) != list(out.grain):
        problems.append(f"output.grain {pipeline.output.grain} differs from recipe output {out.name}'s {out.grain}")
    if sorted(pipeline.output.keys) != sorted(out.keys):
        problems.append(f"output.keys {pipeline.output.keys} differ from recipe output {out.name}'s {out.keys}")


def _joins(pipeline: PipelineSpec, v: ValidatedRecipe, problems: list[str]) -> None:
    for j in pipeline.joins:
        node = v.nodes.get(j.node)
        if not isinstance(node, JoinNode):
            problems.append(f"joins: {j.node} is not a join of recipe {v.recipe.name}")
        elif node.expected_cardinality != j.expected_cardinality:
            problems.append(f"joins: {j.node} is declared {node.expected_cardinality} in the recipe, "
                            f"{j.expected_cardinality} in the pipeline")


def _reconciliations(pipeline: PipelineSpec, v: ValidatedRecipe, out: OutputNode, problems: list[str]) -> None:
    out_cols = {c.name: c.type for c in out.output_schema or []}
    for c in pipeline.checks:
        where = f"checks.{c.name}"
        if c.input_node not in v.schemas:
            problems.append(f"{where}: node {c.input_node} is not in recipe {v.recipe.name}")
            continue
        in_cols = v.columns(c.input_node)
        if c.func == "sum":
            if not c.input_column or not c.output_column:
                problems.append(f"{where}: sum needs input_column and output_column")
                continue
            for col, cols, side in ((c.input_column, in_cols, c.input_node), (c.output_column, out_cols, out.name)):
                if col not in cols:
                    problems.append(f"{where}: {side} has no column {col}")
                elif family(cols[col]) != "numeric":
                    problems.append(f"{where}: {side}.{col} is {cols[col]}; sum reconciles numeric columns")
        else:
            for col, cols, side in ((c.input_column, in_cols, c.input_node), (c.output_column, out_cols, out.name)):
                if col is not None and col not in cols:
                    problems.append(f"{where}: {side} has no column {col}")
