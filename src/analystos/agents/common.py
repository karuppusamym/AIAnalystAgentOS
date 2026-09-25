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


def objective_tokens(text: str | None) -> set[str]:
    return {w.strip(",.?").lower() for w in (text or "").split() if len(w) > 3}


def column_rank(entry: dict[str, Any], tokens: set[str]) -> tuple[int, int]:
    """Sort key, most useful first: relevance to the objective, then analytical usefulness."""
    return -_relevance(entry.get("name", ""), entry.get("meaning", ""), tokens), _SEMANTIC_PRIORITY.get(entry.get("semantic_type") or "", 4)


def table_relevance(table: dict[str, Any], tokens: set[str]) -> int:
    return sum(_relevance(c.get("name", ""), c.get("meaning", ""), tokens) for c in table.get("columns") or [])


def catalog_for_prompt(ctx: RunContext, *, include_values: bool = True) -> list[dict[str, Any]]:
    """Schema-level catalog for prompts. Denied columns are removed. Category vocabularies (top
    values) are only included when the workspace policy allows data samples to reach models
    (`send_data_samples_to_models`, default false) and then only for low-cardinality, non-PII columns.

    Token economy (admin: llm.compact_prompts): tables and columns are ranked by relevance to the
    objective and analytical usefulness, then capped; identifiers and free text go last."""
    from analystos.services.platform_settings import get as platform

    llm = platform().llm
    tokens = objective_tokens(ctx.run.objective) if ctx.run else set()
    samples_allowed = bool(getattr(getattr(ctx, "policy", None), "send_data_samples_to_models", False))
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
            if include_values and samples_allowed and top and "pii" not in (c.tags or []) and (p.get("distinct") or 999) <= 40:
                entry["values"] = [t.get("value") for t in top[:12]]
            entries.append(entry)
        if llm.compact_prompts and len(entries) > llm.catalog_max_columns_per_table:
            entries.sort(key=lambda e: column_rank(e, tokens))
            entries = entries[:llm.catalog_max_columns_per_table]
        catalog.append({"asset": fq, "business_name": asset.business_name, "row_count": asset.row_count, "columns": entries})
    if llm.compact_prompts and len(catalog) > llm.catalog_max_tables:
        catalog.sort(key=lambda t: -table_relevance(t, tokens))
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


def _size(value: Any) -> int:
    return len(compact_json(value)) + 1  # + separator


def fit_payload(payload: dict[str, Any], *, max_chars: int, objective: str | None = None) -> dict[str, Any]:
    """Shrink a prompt payload to `max_chars` of JSON by dropping whole units, never characters.

    Order: whole catalog tables (least relevant to the objective first, at least one kept), then
    whole columns (least useful first, at least one per table kept), then trailing items of other
    top-level lists (largest list first). Everything dropped is listed under `omitted`, so the
    model knows the context is partial. Interim until the context compiler (P4-T03)."""
    if len(compact_json(payload)) <= max_chars:
        return payload
    tokens = objective_tokens(objective if objective is not None else str(payload.get("objective") or payload.get("question") or ""))
    out = dict(payload)
    omitted: dict[str, Any] = {"reason": f"prompt budget {max_chars} chars"}
    out["omitted"] = omitted

    def exact() -> int:
        return len(compact_json(out))

    size = exact()
    catalog = out.get("catalog")
    if isinstance(catalog, list) and all(isinstance(t, dict) for t in catalog):
        catalog = [{**t, "columns": list(t.get("columns") or [])} for t in catalog]
        out["catalog"] = catalog
        # 1. whole tables, least relevant (then latest-listed) first
        order = sorted(range(len(catalog)), key=lambda i: (table_relevance(catalog[i], tokens), -i))
        drop: set[int] = set()
        dropped_tables = omitted.setdefault("catalog_tables", [])
        size = exact()
        for i in order:
            if size <= max_chars or len(drop) >= len(catalog) - 1:
                break
            drop.add(i)
            name = str(catalog[i].get("asset"))
            dropped_tables.append(name)
            size += _size(name) - _size(catalog[i])  # running estimate; re-synced when it says "fits"
            if size <= max_chars:
                out["catalog"] = [t for j, t in enumerate(catalog) if j not in drop]
                size = exact()
        catalog = [t for j, t in enumerate(catalog) if j not in drop]
        out["catalog"] = catalog
        # 2. whole columns, least useful first across the remaining tables
        dropped_cols: dict[str, list[str]] = omitted.setdefault("catalog_columns", {})
        size = exact()
        if size > max_chars:
            ranked = sorted(((ti, c) for ti, t in enumerate(catalog) for c in t["columns"]),
                            key=lambda tc: column_rank(tc[1], tokens), reverse=True)
            for ti, col in ranked:
                if size <= max_chars:
                    break
                table = catalog[ti]
                if len(table["columns"]) <= 1:
                    continue
                table["columns"].remove(col)
                asset = str(table.get("asset"))
                size += _size(col.get("name")) - _size(col) + (0 if asset in dropped_cols else _size(asset) + 3)
                dropped_cols.setdefault(asset, []).append(col.get("name"))
                if size <= max_chars:
                    size = exact()
            size = exact()
        if not omitted.get("catalog_tables"):
            omitted.pop("catalog_tables", None)
        if not omitted.get("catalog_columns"):
            omitted.pop("catalog_columns", None)
        size = exact()
    # 3. trailing items of other lists, largest list first
    while size > max_chars:
        lists = [(k, v) for k, v in out.items() if k not in ("catalog", "omitted") and isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        key, items = max(lists, key=lambda kv: _size(kv[1]))
        out[key] = items[:-1]
        omitted[f"{key}_items_dropped"] = omitted.get(f"{key}_items_dropped", 0) + 1
        size = exact()
    return out


def llm_json(ctx: RunContext, purpose: str, prompt_name: str, payload: dict[str, Any], *,
             exclude_families: list[str] | None = None, max_tokens: int | None = None,
             prompt_vars: dict[str, str] | None = None) -> tuple[Any | None, str | None]:
    """Call a chat model for JSON. Returns (data, model) or (None, reason) — callers degrade visibly.

    The call context carries the workspace policy (provider list, residency, approval threshold)
    and `prompt_version = <name>@<hash of the exact system text>`; the payload is fitted to the
    admin prompt limit by dropping whole entries (fit_payload), never by cutting the JSON."""
    import dataclasses

    from analystos.agents.prompts import prompt, prompt_version_id
    from analystos.llm.cache import estimate_tokens
    from analystos.services.platform_settings import get as platform

    if ctx.router.mode(purpose) == "off":
        model_gate(ctx, purpose, payload, deterministic_ok=True)  # records the avoided call
        return None, "llm_off"
    system = prompt(prompt_name, **(prompt_vars or {}))
    call = dataclasses.replace(ctx.call_ctx(exclude_families=exclude_families), prompt_version=prompt_version_id(prompt_name, system))
    call.with_policy(getattr(ctx, "policy", None))
    if not ctx.router.available(purpose, call):
        return None, "llm_unavailable"
    budget = int(platform().llm.max_prompt_tokens * 3.6) - len(system) - 200  # inverse of estimate_tokens, minus margin
    run = getattr(ctx, "run", None)
    fitted = fit_payload(payload, max_chars=max(2_000, budget), objective=run.objective if run else None)
    if fitted is not payload:
        ctx.say(f"Prompt for {purpose} trimmed to fit the model budget (~{estimate_tokens(compact_json(fitted))} tokens); "
                f"omitted: {compact_json({k: v for k, v in fitted['omitted'].items() if k != 'reason'})[:300]}", kind="decision")
    try:
        response = ctx.router.complete_json(purpose, system, compact_json(fitted), ctx=call, max_tokens=max_tokens)
        return response.data, response.model
    except AnalystOSError as exc:
        log.warning("llm %s failed: %s", purpose, exc)
        ctx.say(f"Model call for {purpose} unavailable ({exc.code}); using deterministic fallback.", kind="decision")
        return None, exc.code
