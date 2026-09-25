from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from analystos.core.errors import AnalystOSError
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import RunTask, SourceAsset, SourceColumn
from analystos.runtime.context import RunContext

log = get_logger(__name__)


def task_output(run_id: str, key: str) -> dict[str, Any]:
    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key))
        return dict(task.output or {}) if task else {}


def asset_rows(ctx: RunContext) -> list[tuple[SourceAsset, list[SourceColumn]]]:
    out = []
    with session_scope() as s:
        for fq in ctx.scope.assets:
            schema, name = fq.split(".", 1)
            asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ctx.workspace.id,
                                                       SourceAsset.schema_name == schema, SourceAsset.name == name))
            if asset is None:
                continue
            cols = list(s.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id).order_by(SourceColumn.ordinal)))
            s.expunge_all()
            out.append((asset, cols))
    return out


def catalog_for_prompt(ctx: RunContext, *, include_values: bool = True) -> list[dict[str, Any]]:
    """Schema-level catalog for prompts. Denied columns are removed. Category vocabularies are only
    included for low-cardinality, non-PII columns (schema-level vocabulary, not record data)."""
    denied = set(ctx.scope.denied_columns)
    catalog = []
    for asset, cols in asset_rows(ctx):
        fq = f"{asset.schema_name}.{asset.name}"
        entries = []
        for c in cols:
            if f"{fq}.{c.name}" in denied:
                continue
            p = c.profile or {}
            entry: dict[str, Any] = {"name": c.name, "type": c.data_type, "semantic_type": c.semantic_type or p.get("semantic_type")}
            if c.business_name or c.description:
                entry["meaning"] = (c.business_name or "") + (f" - {c.description}" if c.description else "")
            if p.get("distinct") is not None:
                entry["distinct"] = p.get("distinct")
            if p.get("null_rate") is not None:
                entry["null_rate"] = round(float(p["null_rate"]), 3)
            for k in ("min", "max", "mean", "p50"):
                if p.get(k) is not None and entry.get("semantic_type") in ("numeric", "datetime"):
                    entry[k] = p[k]
            top = p.get("top_values") or []
            if include_values and top and "pii" not in (c.tags or []) and (p.get("distinct") or 999) <= 40:
                entry["values"] = [t.get("value") for t in top[:12]]
            entries.append(entry)
        catalog.append({"asset": fq, "business_name": asset.business_name, "row_count": asset.row_count, "columns": entries})
    return catalog


def llm_json(ctx: RunContext, purpose: str, prompt_name: str, payload: dict[str, Any], *,
             exclude_families: list[str] | None = None, max_tokens: int | None = None) -> tuple[Any | None, str | None]:
    """Call a chat model for JSON. Returns (data, model) or (None, reason) — callers degrade visibly."""
    from analystos.agents.prompts import prompt

    if not ctx.router.available(purpose, ctx.call_ctx(exclude_families=exclude_families)):
        return None, "llm_unavailable"
    try:
        response = ctx.router.complete_json(purpose, prompt(prompt_name), json.dumps(payload, default=str)[:60_000],
                                            ctx=ctx.call_ctx(exclude_families=exclude_families), max_tokens=max_tokens)
        return response.data, response.model
    except AnalystOSError as exc:
        log.warning("llm %s failed: %s", purpose, exc)
        ctx.say(f"Model call for {purpose} unavailable ({exc.code}); using deterministic fallback.", kind="decision")
        return None, exc.code
