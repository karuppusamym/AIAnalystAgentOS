"""Read-only lookup skills for declarative agents (P4-X03). Each takes the step's RunContext first:
the catalog they read is already scoped (selected assets only, denied columns removed), and the one
that touches data goes through `ctx.run_sql()` (tool gate, query budget, gateway).

Agent policy `pii_access: none` removes PII-tagged columns from what these return, and category
values are only shown when the workspace allows data samples to reach models."""
from __future__ import annotations

from typing import Any

from analystos.core.errors import PolicyDenied


def _in_scope(ctx: Any, asset: str) -> str:
    if asset not in ctx.scope.assets:
        raise PolicyDenied(f"{asset} is outside the authorized scope of this run")
    return asset


def _visible(ctx: Any, fq_asset: str, col: Any) -> bool:
    if f"{fq_asset}.{col.name}" in set(ctx.scope.denied_columns):
        return False
    pii = "pii" in (col.tags or []) or bool((col.semantics or {}).get("pii"))
    return not (pii and ctx.agent.policies.pii_access == "none")


def catalog_lookup(ctx: Any, assets: list[str], max_columns: int = 40) -> dict[str, Any]:
    from analystos.agents.common import asset_rows

    wanted = {_in_scope(ctx, a) for a in assets}
    tables = []
    for asset, cols in asset_rows(ctx):
        fq = f"{asset.schema_name}.{asset.name}"
        if fq not in wanted:
            continue
        visible = [c for c in cols if _visible(ctx, fq, c)]
        tables.append({"asset": fq, "business_name": asset.business_name, "description": asset.description,
                       "role": (asset.semantics or {}).get("role"), "row_count": asset.row_count,
                       "column_count": len(visible), "key_columns": [c.name for c in visible if c.is_key],
                       "columns": [{"name": c.name, "type": c.data_type, "semantic_type": c.semantic_type,
                                    "business_name": c.business_name, "description": c.description}
                                   for c in visible[:max_columns]]})
    return {"tables": tables}


def column_profile_lookup(ctx: Any, asset: str, columns: list[str] | None = None) -> dict[str, Any]:
    from analystos.agents.common import asset_rows

    _in_scope(ctx, asset)
    samples = bool(getattr(ctx.policy, "send_data_samples_to_models", False))
    out = []
    for a, cols in asset_rows(ctx):
        if f"{a.schema_name}.{a.name}" != asset:
            continue
        for c in cols:
            if (columns and c.name not in columns) or not _visible(ctx, asset, c):
                continue
            p = c.profile or {}
            entry = {"name": c.name, "null_rate": p.get("null_rate"), "distinct": p.get("distinct")}
            if c.semantic_type in ("numeric", "datetime"):
                entry.update({k: p.get(k) for k in ("min", "max") if p.get(k) is not None})
            if samples and "pii" not in (c.tags or []) and (p.get("distinct") or 999) <= 40:
                entry["values"] = [t.get("value") for t in (p.get("top_values") or [])[:8]]
            out.append(entry)
    return {"asset": asset, "columns": out}


def row_count(ctx: Any, asset: str) -> dict[str, Any]:
    _in_scope(ctx, asset)
    schema, name = asset.split(".", 1)
    run = ctx.run_sql(ctx.scope.asset_sources.get(asset))
    result = run(f'SELECT COUNT(*) AS n FROM "{schema}"."{name}"', purpose="agent_action",
                 max_rows=min(1, ctx.agent.policies.max_rows_extract))
    rows = getattr(result, "rows", None) or []
    return {"asset": asset, "row_count": int(rows[0][0]) if rows else None}
