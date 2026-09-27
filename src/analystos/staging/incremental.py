"""Watermark incremental staging (P6-02, ADR-0016 decision 1, TRN-003).

A staged table that declares `incremental {watermark, key, late_window, deletes}` in its source config
is refreshed by windows instead of full reloads:

1. **Fixed cursor.** The high watermark is measured once, before any page is read, and the window
   `[last watermark - late_window, high watermark]` is read with keyset pagination on the key (offset
   stays 0), so rows changing while the window is read are never skipped (the dlt spike's trap).
2. **Merge in one transaction.** The window, de-duplicated (the newest watermark per key wins), is merged
   on the key; the new watermark is written by the same transaction, after the rows, into the loader's
   state table in the analytics database. A crash before the commit leaves both the rows and the
   watermark where they were, so the next run re-reads the window instead of skipping it.
3. **Idempotent.** Re-applying a window (a retry, an overlapping schedule) replaces the same keys with the
   same rows, and the watermark only moves forward (max of stored and new), so nothing is double counted.
4. **Deletes.** A watermark cannot see deletes: a full reconcile of the keys (default weekly, or on
   demand) deletes (`reconcile`), stamps (`soft`) or ignores (`ignore`) the keys the source dropped.
5. **Replay and backfill** re-read a closed watermark range and merge it without moving the watermark.

The connector provides `high_watermark(asset, column)`, `extract_window(asset, watermark, since, until,
key, max_rows)` and `extract_keys(asset, keys, max_rows)`; ServiceNow implements them.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pyarrow as pa

from analystos.connectors import sampling
from analystos.connectors.base import DiscoveredAsset
from analystos.contracts.recipe import SOFT_DELETE_COLUMN, Incremental
from analystos.core.errors import InvalidInput
from analystos.core.ids import new_id, utcnow

MODES = ("auto", "full", "reconcile", "replay", "backfill")


def supports(connector: Any) -> bool:
    return all(callable(getattr(connector, n, None)) for n in ("high_watermark", "extract_window", "extract_keys"))


def incremental_for(config: dict[str, Any] | None, *names: str) -> Incremental | None:
    """The `incremental` block a source config declares for a table (by its source or staged name)."""
    block = (config or {}).get("incremental") or {}
    if not isinstance(block, dict):
        raise InvalidInput("config.incremental must map table names to {watermark, key, late_window, deletes}")
    for n in names:
        if n in block:
            try:
                return Incremental.model_validate(block[n])
            except ValueError as exc:
                raise InvalidInput(f"config.incremental.{n} is invalid: {exc}") from None
    return None


def to_watermark(value: Any) -> Any:
    """A stored or measured watermark as a comparable value (datetime or number)."""
    if value is None or isinstance(value, (datetime, int, float, Decimal)):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        pass
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        raise InvalidInput(f"watermark {text!r} is neither a timestamp nor a number") from None


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def shift(value: Any, seconds: int) -> Any:
    """`value` moved back by the late-arrival window (seconds, or units for a numeric watermark)."""
    if value is None or not seconds:
        return value
    if isinstance(value, datetime):
        return value - timedelta(seconds=seconds)
    return value - seconds


def later(a: Any, b: Any) -> Any:
    a, b = to_watermark(a), to_watermark(b)
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _collect(batches: Any, cap: int) -> tuple[pa.Schema | None, list[list[Any]]]:
    schema, rows = None, []
    for batch in batches:
        schema = schema or batch.schema
        cols = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
        rows.extend([list(r) for r in zip(*cols, strict=True)])
        if len(rows) > cap:
            raise InvalidInput(f"the window holds more than {cap} rows, the staging cap; the watermark was not moved. "
                               "Backfill it in smaller watermark ranges or raise the cap.", details={"cap": cap})
    return schema, rows


def dedupe(schema: pa.Schema, rows: list[list[Any]], keys: list[str], watermark: str) -> tuple[list[list[Any]], int]:
    """One row per key: the newest watermark wins, a tie keeps the later row (the source's newest page)."""
    names = schema.names
    missing = [c for c in [*keys, watermark] if c not in names]
    if missing:
        raise InvalidInput(f"incremental column {', '.join(missing)} is not a staged column (have: {', '.join(names)})")
    kidx, widx = [names.index(k) for k in keys], names.index(watermark)
    best: dict[tuple, list[Any]] = {}
    for row in rows:
        k = tuple(row[i] for i in kidx)
        if any(v is None for v in k):
            raise InvalidInput(f"incremental key ({', '.join(keys)}) is null in a row; a merge needs every key")
        cur = best.get(k)
        if cur is None or (row[widx] is not None and (cur[widx] is None or row[widx] >= cur[widx])):
            best[k] = row
    return list(best.values()), len(rows) - len(best)


def _batches(schema: pa.Schema, rows: list[list[Any]], soft: bool) -> tuple[pa.Schema, list[pa.RecordBatch]]:
    fields = list(schema)
    arrays = [pa.array([r[i] for r in rows], type=f.type) for i, f in enumerate(fields)]
    if soft and SOFT_DELETE_COLUMN not in schema.names:
        fields.append(pa.field(SOFT_DELETE_COLUMN, pa.timestamp("us", tz="UTC")))
        arrays.append(pa.array([None] * len(rows), type=pa.timestamp("us", tz="UTC")))
    out = pa.schema(fields)
    return out, [pa.RecordBatch.from_arrays(arrays, schema=out)]


def reconcile_due(state: dict[str, Any] | None, inc: Incremental, now: datetime) -> bool:
    if inc.deletes == "ignore":
        return False
    last = (state or {}).get("last_reconciled_at")
    return last is None or to_watermark(last) <= now - timedelta(hours=inc.reconcile_every_hours)


def stage_incremental(loader: Any, connector: Any, source_id: str, asset: DiscoveredAsset, inc: Incremental, *,
                      workspace_id: str, cap: int, mode: str = "auto", window: tuple[Any, Any] | None = None,
                      now: datetime | None = None) -> dict[str, Any]:
    """Refresh one staged table by watermark. `mode`: `auto` (initial full load, then windows, reconciling
    deletes when due), `full` (re-read everything up to the high watermark and swap), `reconcile` (a window,
    then the full reconcile of deletes), `replay`/`backfill` (merge the closed range `window` without moving
    the watermark). Returns the loader's info plus `snapshot` (with its `incremental` record)."""
    if mode not in MODES:
        raise InvalidInput(f"mode must be one of {', '.join(MODES)}")
    if not supports(connector):
        raise InvalidInput(f"{type(connector).__name__} cannot read by watermark; stage this table in full")
    now = now or utcnow()
    state = loader.read_state(source_id, asset.name)
    stored = to_watermark((state or {}).get("watermark"))
    high = to_watermark(connector.high_watermark(asset, inc.watermark))  # the fixed cursor of this run
    soft = inc.deletes == "soft"
    run: dict[str, Any] = {"mode": mode, "watermark_column": inc.watermark, "key": list(inc.key),
                           "late_window_seconds": inc.late_window, "deletes": inc.deletes, "high_watermark": as_text(high)}

    if mode in ("replay", "backfill"):
        if not window or window[0] is None or window[1] is None:
            raise InvalidInput(f"{mode} needs a watermark range [start, end]")
        if stored is None:
            raise InvalidInput(f"{asset.name} has no incremental state yet; run the initial load before a {mode}")
        since, until = to_watermark(window[0]), to_watermark(window[1])
        if until < since:
            raise InvalidInput(f"the {mode} range ends before it starts")
        load_mode, advance = "merge", False
    elif mode == "full" or stored is None:
        since = to_watermark(inc.backfill.start) if inc.backfill and inc.backfill.start else None
        until, load_mode, advance = high, "replace", True
    else:
        since, until, load_mode, advance = shift(stored, inc.late_window), high, "merge", True
    if until is None and stored is not None and load_mode == "merge":
        until = stored  # the source is empty now; the reconcile below will see it
    run.update(since=as_text(since), until=as_text(until), load_mode=load_mode)

    schema, rows = _collect(connector.extract_window(asset, watermark=inc.watermark, since=since, until=until,
                                                     key=inc.key[0], max_rows=cap), cap)
    run["rows_fetched"] = len(rows)
    deduped = 0
    if schema is not None:
        rows, deduped = dedupe(schema, rows, inc.key, inc.watermark)
    run["deduplicated_rows"] = deduped
    load_id = new_id("load")

    def next_state(old: dict[str, Any] | None) -> dict[str, Any]:
        new = dict(old or {})
        if advance:
            # Only forward: an overlapping or retried window with an older cursor never moves it back.
            new["watermark"] = as_text(later(new.get("watermark"), until)) if load_mode == "merge" else as_text(until)
        new.update(watermark_column=inc.watermark, key=list(inc.key), deletes=inc.deletes, load_id=load_id,
                   last_window={k: run[k] for k in ("mode", "since", "until", "rows_fetched", "deduplicated_rows")},
                   updated_at=now.isoformat())
        if load_mode == "replace":
            new["last_full_at"] = now.isoformat()
            new["last_reconciled_at"] = now.isoformat()  # a full read is a reconcile by construction
        return new

    if rows or load_mode == "replace":
        # an empty table (no schema read): the declared columns give the shape
        batches = _batches(schema, rows, soft)[1] if schema is not None else []
        loaded = loader.load(source_id, asset, batches, workspace_id=workspace_id, mode=load_mode,
                             keys=list(inc.key) if load_mode == "merge" else None, fingerprint="table", state=next_state)
        run["rows_merged"] = loaded.get("rows_loaded", len(rows))
        info = dict(loaded)
    else:
        new_state = loader.save_state(source_id, asset.name, next_state)
        info = {"schema": None, "table": asset.name, "state": new_state, "rows_loaded": 0}
        run["rows_merged"] = 0
    state_now = info.get("state") or {}

    if mode == "reconcile" or (mode == "auto" and load_mode == "merge" and reconcile_due(state_now, inc, now)):
        keys = connector.extract_keys(asset, keys=list(inc.key), max_rows=cap + 1)
        rec = loader.reconcile(source_id, asset.name, keys, keys=list(inc.key), policy=inc.deletes,
                               workspace_id=workspace_id, max_delete_pct=inc.max_delete_pct,
                               state=lambda old: {**(old or {}), "last_reconciled_at": now.isoformat()})
        run["reconcile"] = {k: rec[k] for k in ("policy", "keys_received", "missing_rows", "deleted_rows",
                                                "soft_deleted_rows")}
        info.update({k: rec[k] for k in ("row_count", "content_fingerprint", "schema", "table", "columns")})
        state_now = rec.get("state") or state_now
    if info.get("row_count") is None or info.get("schema") is None:
        info.update(loader.table_info(source_id, asset.name))
    run["watermark"] = state_now.get("watermark")
    rows_now = int(info.get("row_count") or 0)
    snap = sampling.snapshot_record(spec=None, rows_staged=rows_now, cap=cap, truncated=False, source_total_rows=rows_now,
                                    total_basis="incremental_merge")
    snap.update(sampling_method="full", representative=True, incremental=run, staged_at=now.isoformat(), load_id=load_id)
    snap["sampling"]["method"] = "full"
    if info.get("content_fingerprint"):
        snap["content_fingerprint"] = info["content_fingerprint"]
    info["snapshot"], info["truncated"], info["incremental"] = snap, False, run
    return info

