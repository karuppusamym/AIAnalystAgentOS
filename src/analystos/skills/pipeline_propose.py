"""`skill.pipeline_propose` (P6-08): the engineer agent's only action. It proposes recipe nodes and a
PipelineSpec and returns them *validated or rejected*; it never saves, publishes, compiles or runs anything.

Two paths, one gate:
* **rules** (the `off`/`auto` path, `default_actions`): from the catalog of one in-scope table, a staging
  recipe (`source` with every readable column in its IR type -> `dedupe` on the key columns -> `output`
  keyed on them) and its PipelineSpec, incremental by watermark when the table has a key and an
  update-timestamp column;
* **model** (`pipeline_proposal` purpose): the agent passes a proposed `recipe` / `pipeline` object.

Either way the proposal goes through the recipe IR validator (`contracts/recipe.validate_recipe`), the
scope check the executor uses (`recipes/execute.check_scope`: in-scope assets, existing and readable
columns) and the PipelineSpec check (`pipelines/spec.check`) before it is returned as valid; a rejected
proposal comes back with every problem listed, so the next proposal round can fix it (CLAUDE.md rule 3).
"""
from __future__ import annotations

import re
from typing import Any

from analystos.contracts.recipe import RecipeInvalid, canonical_type, family, validate_recipe
from analystos.core.errors import AnalystOSError, PolicyDenied

_COLUMN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_WATERMARK = re.compile(r"(^|_)(updated|modified|changed|last_update)(_|$)")


def _visible(ctx: Any, fq: str, col: Any) -> bool:
    if f"{fq}.{col.name}" in set(ctx.scope.denied_columns):
        return False
    pii = "pii" in (col.tags or []) or bool((col.semantics or {}).get("pii"))
    return not (pii and ctx.agent.policies.pii_access == "none")


def _name(asset: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", asset.split(".", 1)[1].lower()).strip("_") or "table"
    return f"prepare_{base}"[:50]


def rule_proposal(ctx: Any, asset: str) -> dict[str, Any]:
    """A staging recipe and PipelineSpec for one table, derived from the catalog only (no source query)."""
    from analystos.agents.common import asset_rows

    rows = {f"{a.schema_name}.{a.name}": cols for a, cols in asset_rows(ctx)}
    if asset not in rows:
        raise PolicyDenied(f"{asset} is outside the authorized scope of this run")
    columns, skipped = [], []
    for c in rows[asset]:
        ctype = canonical_type(c.data_type)
        if not _visible(ctx, asset, c):
            skipped.append({"column": c.name, "reason": "not readable under the policy or the agent's pii_access"})
        elif ctype is None or not _COLUMN.match(c.name) or c.name.startswith("aos_"):
            skipped.append({"column": c.name, "reason": f"type {c.data_type} or name is outside the recipe IR"})
        else:
            columns.append({"name": c.name, "type": ctype, "key": bool(c.is_key)})
    keys = [c["name"] for c in columns if c["key"]]
    schema = [{"name": c["name"], "type": c["type"]} for c in columns]
    name = _name(asset)
    nodes: list[dict[str, Any]] = [{"op": "source", "id": "src", "asset": asset, "schema": schema}]
    last = "src"
    if keys:
        wm = next((c["name"] for c in columns if _WATERMARK.search(c["name"]) and family(c["type"]) == "temporal"), None)
        order = [{"column": wm, "desc": True}] if wm else []
        nodes.append({"op": "dedupe", "id": "one_per_key", "input": "src", "keys": keys, "order": order})
        last = "one_per_key"
    else:
        wm = None
    nodes.append({"op": "output", "id": "out", "input": last, "name": "staged", "grain": keys, "keys": keys,
                  "schema": schema, "schema_policy": "warn"})
    recipe = {"kind": "Recipe", "name": name, "description": f"Staging copy of {asset}, one row per key", "nodes": nodes}
    pipeline: dict[str, Any] = {
        "type": "pipeline", "name": name, "recipes": [{"name": name}], "inputs": [{"asset": asset}],
        "output": {"recipe": name, "output": "staged", "grain": keys, "keys": keys, "schema": schema}}
    if keys and wm:
        pipeline["incremental"] = {"watermark": wm, "key": keys, "late_window": 900, "deletes": "reconcile"}
    return {"recipe": recipe, "pipeline": pipeline, "source": "rules", "skipped_columns": skipped}


def check_proposal(ctx: Any, recipe: dict[str, Any], pipeline: dict[str, Any] | None) -> dict[str, Any]:
    """The gate every proposal passes before it may be compiled or saved by a person."""
    from analystos.pipelines import spec as pipeline_spec
    from analystos.recipes.execute import check_scope

    problems: list[str] = []
    stamped, spec_hash, pipeline_out = None, None, None
    try:
        v = validate_recipe(recipe)
        check_scope(v, ctx.scope)
        stamped, spec_hash = v.stamped(), v.hash
    except RecipeInvalid as exc:
        problems += exc.problems
    except AnalystOSError as exc:
        problems.append(exc.message)
    if pipeline is not None and not problems:
        try:
            p = pipeline_spec.parse(pipeline)
            name = stamped["name"]
            pipeline_spec.check(p, {name: stamped})
            pipeline_out = p.spec()
        except pipeline_spec.PipelineInvalid as exc:
            problems += exc.problems
        except AnalystOSError as exc:
            problems.append(exc.message)
    return {"valid": not problems, "problems": problems, "recipe": stamped if not problems else recipe,
            "recipe_hash": spec_hash, "pipeline": pipeline_out if not problems else pipeline, "compiled": False}


def pipeline_propose(ctx: Any, assets: list[str] | None = None, recipe: dict[str, Any] | None = None,
                     pipeline: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validated proposals: the given `recipe`/`pipeline` (a model's), or one rule proposal per asset."""
    proposals = []
    if recipe is not None:
        proposals.append({"source": "proposed", **check_proposal(ctx, recipe, pipeline)})
    else:
        for asset in (assets or [])[:10]:
            rule = rule_proposal(ctx, asset)
            proposals.append({"source": "rules", "asset": asset, "skipped_columns": rule["skipped_columns"],
                              **check_proposal(ctx, rule["recipe"], rule["pipeline"])})
    # problems first: the generic runtime summarises results back to the next proposal round, truncated
    return {"valid": sum(p["valid"] for p in proposals), "rejected": sum(not p["valid"] for p in proposals),
            "problems": [q for p in proposals for q in p["problems"]], "proposals": proposals,
            "note": "Proposals only: nothing was saved, compiled or run. A person saves and publishes the recipe and "
                    "the pipeline; the dry run and any materialization follow their own checks and approval."}
