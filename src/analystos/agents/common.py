from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from analystos.core.errors import AnalystOSError, ContextOverBudget
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


def column_rank(entry: dict[str, Any] | str, tokens: set[str]) -> tuple[int, int]:
    """Sort key, most useful first: relevance to the objective, then analytical usefulness."""
    if not isinstance(entry, dict):  # names-only catalog digest (context compiler)
        entry = {"name": str(entry)}
    return -_relevance(entry.get("name", ""), entry.get("meaning", ""), tokens), _SEMANTIC_PRIORITY.get(entry.get("semantic_type") or "", 4)


def table_relevance(table: dict[str, Any], tokens: set[str]) -> int:
    return sum(_relevance(c.get("name", ""), c.get("meaning", ""), tokens) if isinstance(c, dict) else _relevance(str(c), "", tokens)
               for c in table.get("columns") or [])


def catalog_for_prompt(ctx: RunContext, *, include_values: bool = True, objective: str | None = None,
                       capped: bool = True) -> list[dict[str, Any]]:
    """Schema-level catalog for prompts. Denied columns are removed. Category vocabularies (top
    values) are only included when the workspace policy allows data samples to reach models
    (`send_data_samples_to_models`, default false) and then only for low-cardinality, non-PII columns.

    Token economy (admin: llm.compact_prompts): tables and columns are ranked by relevance to the
    objective and analytical usefulness, then capped; identifiers and free text go last. The context
    compiler asks for the uncapped catalog (`capped=False`) and applies its purpose profile's caps
    itself, so what it leaves out is listed as omitted."""
    from analystos.services.platform_settings import get as platform

    llm = platform().llm
    tokens = objective_tokens(objective if objective is not None else (ctx.run.objective if ctx.run else None))
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
        if capped and llm.compact_prompts and len(entries) > llm.catalog_max_columns_per_table:
            entries.sort(key=lambda e: column_rank(e, tokens))
            entries = entries[:llm.catalog_max_columns_per_table]
        catalog.append({"asset": fq, "business_name": asset.business_name, "row_count": asset.row_count, "columns": entries})
    if capped and llm.compact_prompts and len(catalog) > llm.catalog_max_tables:
        catalog.sort(key=lambda t: -table_relevance(t, tokens))
        catalog = catalog[:llm.catalog_max_tables]
    return catalog


def model_gate(ctx: RunContext, purpose: str, payload: Any, *, deterministic_ok: bool) -> bool:
    """Should this step call a model? `off` never; `auto` only when the deterministic result is not
    good enough; `always` whenever available. Avoided calls are recorded as tokens saved."""
    from analystos.llm.cache import estimate_tokens

    mode = ctx.router.mode(purpose)
    if mode == "off" or (mode == "auto" and deterministic_ok):
        ctx.router.record_skip(purpose, ctx.call_ctx(), estimated_tokens=estimate_tokens(_prompt_text(payload)) + 500,
                               reason=f"mode={mode}: deterministic result used")
        return False
    return True


def compact_json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False)


def _prompt_text(payload: Any) -> str:
    from analystos.context.compiler import CompiledContext

    if isinstance(payload, CompiledContext):
        return compact_json(payload.header) + compact_json(payload.body)
    return compact_json(payload)


_SCOPE_CATALOG: Any = object()


def context_header(ctx: Any) -> dict[str, Any]:
    """Workspace header (P4-T04): stable per workspace and scope, so it sits in the cached prompt
    prefix right after the static system text. Nothing objective- or run-specific goes here."""
    header: dict[str, Any] = {}
    workspace = getattr(ctx, "workspace", None)
    if getattr(workspace, "name", None):
        header["workspace"] = workspace.name
    scope = getattr(ctx, "scope", None)
    if scope is not None:
        try:
            from analystos.capabilities import packs

            refs = sorted(p.ref for p in packs.for_scope(scope, getattr(ctx, "policy", None)))
            if refs:
                header["domain_packs"] = refs
        except Exception as exc:  # the header is a convenience; a pack registry error must not block a call
            log.warning("context header: domain packs unavailable: %s", exc)
        dialects = sorted(set((getattr(scope, "source_dialects", None) or {}).values()))
        if dialects:
            header["dialects"] = dialects
    return header


def compile_for(ctx: Any, purpose: str, required: dict[str, Any], *, objective: str | None = None,
                catalog: Any = _SCOPE_CATALOG, reference_text: str | None = None) -> Any:
    """Compile the prompt context for one model call (P4-T03) from the caller's mandatory inputs,
    the authorized catalog and the workspace knowledge, under the purpose profile's budget.

    Returns a CompiledContext for `llm_json`. When the mandatory part alone is over budget the
    context comes back `refused` (never cut): `llm_json` then records the refusal and the caller
    takes its deterministic path."""
    from analystos.context.compiler import CompiledContext
    from analystos.contracts.platform import PurposeProfile
    from analystos.services.platform_settings import get as platform

    settings = platform()
    profile = settings.context.profiles.get(purpose) or PurposeProfile(max_chars=1_500_000)
    run = getattr(ctx, "run", None)
    if objective is None:
        objective = getattr(run, "objective", None) or str(required.get("objective") or required.get("question") or "")
    if catalog is _SCOPE_CATALOG:
        catalog = (catalog_for_prompt(ctx, include_values=profile.catalog_detail == "profile", objective=objective, capped=False)
                   if "catalog" in profile.sections else None)
    if not settings.context.compiler_enabled:  # admin kill switch: the increment-3 payload, fit_payload only
        return CompiledContext(purpose=purpose, header={}, body={**required, **({"catalog": catalog} if catalog else {})})
    header = context_header(ctx)
    limit = int(settings.llm.max_prompt_tokens * 3.6) - _SYSTEM_RESERVE_CHARS
    key = _context_key(ctx, purpose, profile, settings, objective=objective, required=required, catalog=catalog,
                       reference_text=reference_text, header=header, limit=limit)
    reused = _COMPILED.get(key, purpose) if key else None
    if reused is not None:
        return reused
    compiled, complete = _compile(ctx, purpose, profile, settings, objective=objective, required=required, catalog=catalog,
                                  reference_text=reference_text, header=header, limit=limit)
    if key and complete:  # a context compiled without its knowledge (load failed) is not kept
        _COMPILED.put(key, purpose, compiled)
    return compiled


def _compile(ctx: Any, purpose: str, profile: Any, settings: Any, *, objective: str, required: dict[str, Any], catalog: Any,
             reference_text: str | None, header: dict[str, Any], limit: int) -> tuple[Any, bool]:
    from analystos.context.compiler import KNOWLEDGE_SECTIONS, CompiledContext, compile_context, load_knowledge, terms

    run = getattr(ctx, "run", None)
    knowledge: list = []
    complete = True
    sections = [x for x in profile.sections if x in KNOWLEDGE_SECTIONS]
    workspace_id = getattr(getattr(ctx, "workspace", None), "id", None)
    if sections and workspace_id:
        try:
            with session_scope() as s:
                knowledge = load_knowledge(s, workspace_id, sections, run_id=getattr(run, "id", None),
                                           query=" ".join(x for x in (objective, reference_text) if x) or None)
        except Exception as exc:  # knowledge is optional context; its sections then say NO_MATCH
            log.warning("context compiler: knowledge unavailable for %s: %s", purpose, exc)
            complete = False
    generic = {w for a in (getattr(getattr(ctx, "scope", None), "assets", None) or []) for w in terms(a.split(".")[-1])}
    try:
        return compile_context(purpose, profile, objective=objective, required=required, catalog=catalog,
                               knowledge=knowledge, header=header, reference_text=reference_text,
                               limit_chars=max(2_000, limit), min_relevance=settings.context.min_relevance,
                               generic_terms=generic), complete
    except ContextOverBudget as exc:
        return CompiledContext(purpose=purpose, header={}, body=dict(required), refused=exc.message,
                               budget_chars=int(exc.details.get("budget_chars") or 0),
                               mandatory_chars=int(exc.details.get("mandatory_chars") or 0)), complete


_SYSTEM_RESERVE_CHARS = 8_000  # room for the static system text inside max_prompt_tokens (fit_payload re-checks exactly)


# ------------------------------------------------------------------------------ compiled-context reuse (CTX-005)
COMPILED_CONTEXT_TTL_SECONDS = 900
COMPILED_CONTEXT_MAX_ENTRIES = 256


class CompiledContextCache:
    """Compiled contexts reused within one run or Ask thread (CTX-005).

    Key: (purpose, workspace knowledge version, scope hash, inputs hash), and the run or thread it
    was compiled for. The knowledge version covers the context entries, the knowledge packs the
    workspace sees, the enabled domain packs and the platform settings (so an edit to any of them
    compiles afresh); the scope hash covers what the caller may see; the inputs hash covers the
    mandatory inputs, the objective and reference text, the catalog as sent, the purpose profile and
    the budget. Knowledge that changes without a version (verified findings of *other* runs, which a
    prompt may cite) is bounded by the run/thread and a TTL. A context without a run or thread, or
    whose knowledge version cannot be computed, is never cached. Hits return a copy."""

    def __init__(self, ttl: float = COMPILED_CONTEXT_TTL_SECONDS, max_entries: int = COMPILED_CONTEXT_MAX_ENTRIES) -> None:
        import threading
        from collections import OrderedDict

        self.ttl, self.max_entries = ttl, max_entries
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.stats: dict[str, dict[str, int]] = {}

    def _count(self, purpose: str, what: str, chars: int = 0) -> None:
        row = self.stats.setdefault(purpose, {"compiled": 0, "reused": 0, "chars_reused": 0})
        row[what] += 1
        row["chars_reused"] += chars

    def get(self, key: str, purpose: str) -> Any | None:
        import copy
        import time

        with self._lock:
            item = self._items.get(key)
            if item is None or time.monotonic() - item[0] > self.ttl:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            self._count(purpose, "reused", item[1].chars)
            return copy.deepcopy(item[1])

    def put(self, key: str, purpose: str, compiled: Any) -> None:
        import copy
        import time

        with self._lock:
            self._count(purpose, "compiled")
            self._items[key] = (time.monotonic(), copy.deepcopy(compiled))
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.stats = {}


_COMPILED = CompiledContextCache()


def compiled_context_stats() -> dict[str, dict[str, int]]:
    """Per purpose: contexts compiled (misses, stored) and reused (hits), with the characters reused."""
    return {p: dict(v) for p, v in _COMPILED.stats.items()}


def _context_key(ctx: Any, purpose: str, profile: Any, settings: Any, *, objective: str, required: dict[str, Any],
                 catalog: Any, reference_text: str | None, header: dict[str, Any], limit: int) -> str | None:
    from analystos.context.version import workspace_knowledge_version
    from analystos.core.ids import stable_hash

    run = getattr(ctx, "run", None)
    session = f"run:{run.id}" if getattr(run, "id", None) else (
        f"thread:{ctx.thread_id}" if getattr(ctx, "thread_id", None) else None)
    workspace_id = getattr(getattr(ctx, "workspace", None), "id", None)
    if session is None or workspace_id is None:
        return None
    knowledge = workspace_knowledge_version(workspace_id, policy=getattr(ctx, "policy", None))
    if knowledge is None:
        return None
    scope = getattr(ctx, "scope", None)
    try:
        scope_hash = scope.scope_hash() if hasattr(scope, "scope_hash") else stable_hash(compact_json(scope))
        inputs = stable_hash({"required": required, "objective": objective, "reference_text": reference_text,
                              "catalog": catalog, "header": header, "profile": profile.model_dump(mode="json"),
                              "limit": limit, "min_relevance": settings.context.min_relevance})
    except (TypeError, ValueError):  # an input that cannot be hashed canonically is simply not cached
        return None
    return stable_hash({"purpose": purpose, "knowledge": knowledge, "scope": scope_hash, "inputs": inputs, "session": session})


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
    if isinstance(out.get("omitted"), dict):  # the context compiler's own omissions are kept
        omitted.update({k: v for k, v in out["omitted"].items() if k != "reason"})
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
                name = col.get("name") if isinstance(col, dict) else col
                size += _size(name) - _size(col) + (0 if asset in dropped_cols else _size(asset) + 3)
                dropped_cols.setdefault(asset, []).append(name)
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


def _agent_refusal(ctx: Any, purpose: str) -> tuple[str, str] | None:
    """Agent manifest enforcement (P4-X03): only the agent's declared model purposes are routed, and
    only while its per-step budget (`llm_calls`, `usd`) lasts. Contexts without a manifest (Ask,
    monitors) are governed by the workspace policy alone."""
    manifest = getattr(ctx, "manifest", None)
    if manifest is None or manifest.kind != "Agent":
        return None
    from analystos.capabilities.agents import body

    if purpose not in body(manifest).purposes:
        return "purpose_not_declared", (f"Model purpose {purpose} is not declared by {manifest.ref}; "
                                        "using the deterministic path.")
    for kind in ("llm_calls", "usd"):
        if not ctx.budget_left(kind):
            return "agent_budget_exhausted", (f"{manifest.ref} {kind} budget ({getattr(ctx.budget, kind)}) is used up "
                                              f"for this step; {purpose} uses the deterministic path.")
    return None


def _record_context_refusal(ctx: Any, purpose: str, call: Any, reason: str, estimated: int) -> None:
    """A compiler refusal is a model call that did not happen: record it like the router's own
    oversize refusal (status `refused`), so it shows in the call log and the savings ledger."""
    sink = getattr(ctx.router, "sink", None)
    if sink is not None:
        sink.record(ctx=call, purpose=purpose, profile="-", provider="context_compiler", model="-", status="refused",
                    attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=None,
                    error=reason[:500], tokens_saved=estimated)


class ModelOutcome(str):
    """Why `llm_json` returned no data: a reason code (the string itself, so callers that log or
    compare the old second element keep working) plus the message and details of the one cause.

    Codes: mode_off, no_api_key, provider_cooldown (details.retry_in_s), policy_blocked,
    residency_blocked, approval_required, budget_exceeded, cap_reached, context_over_budget,
    invalid_output, purpose_not_declared, agent_budget_exhausted, upstream_unavailable, no_route."""

    message: str
    details: dict[str, Any]

    def __new__(cls, code: str, message: str = "", **details: Any) -> ModelOutcome:
        out = super().__new__(cls, code)
        out.message = message or code
        out.details = details
        return out

    @property
    def code(self) -> str:
        return str.__str__(self)


_OUTCOME_BY_ERROR = {"llm_disabled": "mode_off", "provider_quota_exhausted": "provider_cooldown",
                     "spend_cap_reached": "cap_reached", "spend_counters_unavailable": "cap_reached",
                     "model_route_unavailable": "no_route", "egress_blocked": "policy_blocked"}


def model_outcome(exc: AnalystOSError) -> ModelOutcome:
    """The reason code of a router error (see `ModelOutcome`), keeping its message and details."""
    code = _OUTCOME_BY_ERROR.get(exc.code, exc.code)
    if code == "mode_off" and exc.details.get("oversize"):
        code = "context_over_budget"
    elif code == "budget_exceeded" and exc.details.get("cap"):
        code = "cap_reached"
    elif exc.code == "upstream_unavailable":
        code = "upstream_unavailable"
    return ModelOutcome(code, exc.message, **exc.details)


def llm_json(ctx: RunContext, purpose: str, prompt_name: str, payload: Any, *,
             exclude_families: list[str] | None = None, max_tokens: int | None = None,
             prompt_vars: dict[str, str] | None = None, validate: Callable[[Any], str | None] | None = None,
             escalate: str | None = None, escalated_from: str | None = None) -> tuple[Any | None, str | None]:
    """Call a chat model for JSON. Returns (data, model) or (None, ModelOutcome) — callers degrade
    visibly, and can say which of the causes in `ModelOutcome` stopped the call.

    Cheap first, escalate: `validate(data)` is the caller's deterministic check (None = usable, else
    why not). A small-tier answer that fails it - or is not JSON - is re-asked once of the large tier
    when the purpose's escalation policy allows (router.complete). `escalate` (with the failing model
    as `escalated_from`) asks the large tier directly after a check further downstream failed; an
    answer that still fails validation is returned all the same and the caller's own checks decide.

    `payload` is a CompiledContext (`compile_for`, P4-T03) or, for purposes without run context
    (narrative, verification, summary), a plain dict. The prompt is laid out for provider prompt
    caching (P4-T04): static system text (with the method vocabulary) → workspace header → the
    volatile compiled context last; the first two are marked as the stable prefix.

    The call context carries the workspace policy (provider list, residency, approval threshold),
    `prompt_version = <name>@<hash of the exact system text>`, the compiler's receipts and the
    workspace knowledge version (L0 cache key, P4-T06); the volatile part is fitted to the admin
    prompt limit by dropping whole entries (fit_payload, the final guard), never by cutting JSON."""
    import dataclasses

    from analystos.agents.prompts import prompt, prompt_version_id
    from analystos.context.compiler import CompiledContext
    from analystos.llm.cache import estimate_tokens
    from analystos.services.platform_settings import get as platform

    compiled = payload if isinstance(payload, CompiledContext) else None
    body: dict[str, Any] = compiled.body if compiled is not None else payload
    if ctx.router.mode(purpose) == "off":
        model_gate(ctx, purpose, payload, deterministic_ok=True)  # records the avoided call
        return None, ModelOutcome("mode_off", f"model use for '{purpose}' is turned off by the administrator", purpose=purpose)
    refused = _agent_refusal(ctx, purpose)
    if refused:
        ctx.say(refused[1], kind="decision")
        ctx.router.record_skip(purpose, ctx.call_ctx(), estimated_tokens=estimate_tokens(_prompt_text(payload)) + 500,
                               reason=refused[1][:200])
        return None, ModelOutcome(refused[0], refused[1], purpose=purpose)
    system = prompt(prompt_name, **(prompt_vars or {}))
    call = dataclasses.replace(ctx.call_ctx(exclude_families=exclude_families), prompt_version=prompt_version_id(prompt_name, system))
    call.with_policy(getattr(ctx, "policy", None))
    if compiled is not None and compiled.refused:
        message = (f"Context for {purpose} not sent: {compiled.refused} (mandatory {compiled.mandatory_chars} chars > "
                   f"budget {compiled.budget_chars}); using the deterministic path.")
        ctx.say(message, kind="decision", data={"purpose": purpose, "mandatory_chars": compiled.mandatory_chars,
                                                "budget_chars": compiled.budget_chars})
        _record_context_refusal(ctx, purpose, call, message, estimate_tokens(compact_json(body)) + len(system) // 4)
        return None, ModelOutcome("context_over_budget", message, purpose=purpose, mandatory_chars=compiled.mandatory_chars,
                                  budget_chars=compiled.budget_chars)
    if not ctx.router.available(purpose, call):
        why = getattr(ctx.router, "unavailable", lambda *_: None)(purpose, call)
        return None, (model_outcome(why) if why is not None else ModelOutcome("no_route", f"no model route for {purpose}"))
    llm = platform().llm
    if call.knowledge_version is None and call.workspace_id and llm.cache_enabled and purpose in llm.cacheable_purposes:
        from analystos.context.version import workspace_knowledge_version

        call.knowledge_version = workspace_knowledge_version(call.workspace_id, policy=getattr(ctx, "policy", None))
    header = compiled.header if compiled is not None else {}
    header_text = compact_json({"workspace_context": header}) if header else ""
    budget = int(llm.max_prompt_tokens * 3.6) - len(system) - len(header_text) - 200  # inverse of estimate_tokens, minus margin
    run = getattr(ctx, "run", None)
    fitted = fit_payload(body, max_chars=max(2_000, budget), objective=run.objective if run else None)
    if fitted is not body:
        ctx.say(f"Prompt for {purpose} trimmed to fit the model budget (~{estimate_tokens(compact_json(fitted))} tokens); "
                f"omitted: {compact_json({k: v for k, v in fitted['omitted'].items() if k != 'reason'})[:300]}", kind="decision")
    if compiled is not None:
        call.context_receipts = compiled.receipts
    messages: list[dict[str, Any]] = [{"role": "system", "content": system, "cache": True}]
    if header_text:
        messages.append({"role": "user", "content": header_text, "cache": True})
    messages.append({"role": "user", "content": compact_json(fitted)})
    spend = getattr(ctx, "spend", None)
    if spend is not None:
        spend("llm_calls")
    check = (lambda r: validate(r.data)) if validate is not None else None
    try:
        response = ctx.router.complete(purpose, messages, ctx=call, json_output=True, max_tokens=max_tokens,
                                       validate=check, escalate=escalate, escalated_from=escalated_from)
        if spend is not None:
            spend("usd", response.cost_usd)
        if response.escalated_from:
            ctx.say(f"{purpose}: the answer of {response.escalated_from} failed validation ({response.escalation_reason}); "
                    f"escalated to {response.model}.", kind="decision",
                    data={"purpose": purpose, "escalated_from": response.escalated_from, "model": response.model,
                          "reason": response.escalation_reason})
        return response.data, response.model
    except AnalystOSError as exc:
        log.warning("llm %s failed: %s", purpose, exc)
        if exc.code == "escalation_unavailable":  # the caller's own checks already decided; nothing else was lost
            ctx.say(f"{purpose}: no larger model to escalate to ({exc.message}).", kind="decision")
            return None, model_outcome(exc)
        remedy = exc.details.get("remedy") if isinstance(exc.details, dict) else None
        ctx.say(f"Model call for {purpose} unavailable ({exc.code}); using deterministic fallback."
                + (f" {remedy}" if remedy else ""), kind="decision",
                data={"purpose": purpose, "code": exc.code, **({"remedy": remedy} if remedy else {})})
        return None, model_outcome(exc)
