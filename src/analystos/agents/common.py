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


_SEMANTIC_PRIORITY = {"boolean": 0, "datetime": 1, "categorical": 2, "numeric": 3, "text": 5, "id": 6}


def _relevance(name: str, meaning: str, objective_tokens: set[str]) -> int:
    tokens = set(name.lower().replace("_", " ").split()) | set((meaning or "").lower().split())
    return len(tokens & objective_tokens)


def catalog_for_prompt(ctx: RunContext, *, include_values: bool = True) -> list[dict[str, Any]]:
    """Schema-level catalog for prompts. Denied columns are removed. Category vocabularies are only
    included for low-cardinality, non-PII columns (schema-level vocabulary, not record data).

    Token economy (admin: llm.compact_prompts): tables and columns are ranked by relevance to the
    objective and analytical usefulness, then capped; identifiers and free text go last."""
    from analystos.services.platform_settings import get as platform

    llm = platform().llm
    objective_tokens = {w.strip(",.?").lower() for w in ctx.run.objective.split() if len(w) > 3} if ctx.run else set()
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
        if llm.compact_prompts and len(entries) > llm.catalog_max_columns_per_table:
            entries.sort(key=lambda e: (-_relevance(e["name"], e.get("meaning", ""), objective_tokens),
                                        _SEMANTIC_PRIORITY.get(e.get("semantic_type") or "", 4)))
            entries = entries[:llm.catalog_max_columns_per_table]
        catalog.append({"asset": fq, "business_name": asset.business_name, "row_count": asset.row_count, "columns": entries})
    if llm.compact_prompts and len(catalog) > llm.catalog_max_tables:
        catalog.sort(key=lambda t: -sum(_relevance(c["name"], c.get("meaning", ""), objective_tokens) for c in t["columns"]))
        catalog = catalog[:llm.catalog_max_tables]
    return catalog


def model_gate(ctx: RunContext, purpose: str, payload: Any, *, deterministic_ok: bool) -> bool:
    """Should this step call a model? `off` never; `auto` only when the deterministic result is not
    good enough; `always` whenever available. Avoided calls are recorded as tokens saved."""
    from analystos.llm.cache import estimate_tokens

    mode = ctx.router.mode(purpose)
    if mode == "off" or (mode == "auto" and deterministic_ok):
        ctx.router.record_skip(purpose, ctx.call_ctx(), estimated_tokens=estimate_tokens(compact_json(payload)) + 500,
                               reason=f"mode={mode}: deterministic result used")
        return False
    return True


def compact_json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False)


def llm_json(ctx: RunContext, purpose: str, prompt_name: str, payload: dict[str, Any], *,
             exclude_families: list[str] | None = None, max_tokens: int | None = None) -> tuple[Any | None, str | None]:
    """Call a chat model for JSON. Returns (data, model) or (None, reason) — callers degrade visibly."""
    from analystos.agents.prompts import prompt

    if ctx.router.mode(purpose) == "off":
        model_gate(ctx, purpose, payload, deterministic_ok=True)  # records the avoided call
        return None, "llm_off"
    if not ctx.router.available(purpose, ctx.call_ctx(exclude_families=exclude_families)):
        return None, "llm_unavailable"
    try:
        response = ctx.router.complete_json(purpose, prompt(prompt_name), compact_json(payload)[:60_000],
                                            ctx=ctx.call_ctx(exclude_families=exclude_families), max_tokens=max_tokens)
        return response.data, response.model
    except AnalystOSError as exc:
        log.warning("llm %s failed: %s", purpose, exc)
        ctx.say(f"Model call for {purpose} unavailable ({exc.code}); using deterministic fallback.", kind="decision")
        return None, exc.code
