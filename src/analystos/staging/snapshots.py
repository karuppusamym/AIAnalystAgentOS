"""Snapshot population: stage an asset under its declared sampling strategy, and turn what the
snapshot is into a finding caveat and the ``representative_population`` verification check (P4-C12).

A statistical claim is about the population that was sampled. A staged snapshot that hit the row
cap without a declared strategy is an arbitrary subset (often the oldest partitions), so a finding
computed on it can be reproducible and still describe the wrong population. The snapshot record
(``connectors/sampling.py:snapshot_record``) is stored on ``SourceAsset.snapshot``; findings read it
through ``population_for``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from analystos.connectors import sampling
from analystos.connectors.base import DiscoveredAsset
from analystos.core.ids import utcnow

CHECK = "representative_population"


def stage_asset(loader: Any, connector: Any, source_id: str, asset: DiscoveredAsset, *, config: dict[str, Any] | None,
                platform_max: int, workspace_id: str) -> dict[str, Any]:
    """Extract + load one asset under its declared strategy; the result carries ``snapshot``."""
    spec = sampling.sampling_for(config, asset.source_name, asset.name)
    cap = sampling.row_cap(config, platform_max, spec)
    info = loader.load(source_id, asset, connector.extract(asset, max_rows=cap),
                       workspace_id=workspace_id, snapshot=lambda: getattr(connector, "last_snapshot", None))
    if "snapshot" not in info:  # a connector that does not describe its snapshot: truncation is inferred
        rows = int(info.get("row_count") or 0)
        info["snapshot"] = sampling.snapshot_record(spec=spec, rows_staged=rows, cap=cap, truncated=rows >= cap,
                                                    source_total_rows=None, total_basis="unavailable")
        info["truncated"] = info["snapshot"]["truncated"]
    info["snapshot"]["staged_at"] = utcnow().isoformat()
    return info


def _human(n: int | None) -> str:
    if n is None:
        return "an unknown number of"
    if n >= 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n:,}"


def _pct(value: Any) -> str:
    return f"{float(value):g}%"


def population_caveat(snapshot: dict[str, Any] | None) -> str | None:
    """One sentence saying which population a finding describes; None when it is the whole table."""
    if not snapshot:
        return None
    method = snapshot.get("sampling_method") or sampling.UNDECLARED
    s = snapshot.get("sampling") or {}
    rows, cap = int(snapshot.get("rows_staged") or 0), snapshot.get("row_cap")
    total, truncated = snapshot.get("source_total_rows"), bool(snapshot.get("truncated"))
    estimated = str(snapshot.get("total_rows_basis") or "").startswith("estimate")
    of = f" of {_human(total)}{' (estimated)' if estimated else ''} rows" if total is not None else ""
    if method == "tablesample":
        how = f"{s.get('sampler', 'random')}{'' if s.get('native', True) else ', emulated'}"
        text = f"Computed on a {rows:,}-row {_pct(s.get('percent'))} random sample ({how}){of}."
        if truncated:
            text += (f" The sample exceeded the {int(cap or 0):,}-row cap and was cut short, so it is not a uniform "
                     "sample of the table.")
        return text
    if method == "time_window":
        start, end = str(s.get("window_start") or "")[:10], str(s.get("window_end") or "")[:10]
        span = f", {start} to {end}" if start and end else ""
        text = (f"Computed on a {rows:,}-row time-window sample (last {sampling.window_label(s.get('window') or '')} "
                f"by {s.get('column')}{span}){of}.")
        if truncated:
            pop = snapshot.get("population_rows")
            since = str(s.get("effective_window_start") or "")[:10]
            text += (f" The window held {_human(pop)} rows; only the most recent {rows:,}"
                     f"{' (from ' + since + ')' if since else ''} were staged.")
        return text
    if not truncated:
        return None
    if method == "first_n":
        why = "the first rows the source returned (declared first_n), not a sample"
    elif method == "full":
        why = f"the table exceeded the administrator's {int(cap or 0):,}-row limit, so the snapshot is an unordered subset"
    else:
        why = (f"an arbitrary, unordered subset cut at the {int(cap or 0):,}-row cap with no declared sampling "
               "strategy")
    return f"Computed on {rows:,} rows{of}: {why}; results may not represent the table."


def population_check(mode: str, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """The ``representative_population`` REV check. Fails on truncation without a declared sample
    (undeclared, ``full`` over the limit, ``first_n``) and on a random sample cut short by the cap;
    passes for a time window, an uncut random sample, the whole table, and pushdown sources."""
    if mode == "pushdown":
        return {"check": CHECK, "passed": True, "method": "pushdown", "detail": "queried in place: the whole table"}
    if mode != "staged":
        return {"check": CHECK, "passed": True, "method": "unknown", "detail": "asset source not found; population unknown"}
    if not snapshot:
        return {"check": CHECK, "passed": True, "method": "unknown", "detail": "no snapshot record for this asset"}
    method = snapshot.get("sampling_method") or sampling.UNDECLARED
    s = snapshot.get("sampling") or {}
    params = {k: s[k] for k in ("percent", "sampler", "seed", "native", "column", "window", "window_start", "window_end")
              if k in s}
    total = snapshot.get("source_total_rows")
    size = f"{int(snapshot.get('rows_staged') or 0):,} of {'?' if total is None else f'{int(total):,}'} rows"
    truncated = bool(snapshot.get("truncated"))
    passed = method == "time_window" or not truncated
    if method == "time_window":
        verdict = "defined population (most recent window)" + ("; capped to the newest rows" if truncated else "")
    elif not truncated:
        verdict = "not truncated"
    elif method == "tablesample":
        verdict = "random sample cut short by the row cap"
    elif method == "first_n":
        verdict = "truncated: first_n is not representative"
    else:
        verdict = f"truncated without a declared sampling strategy ({method})"
    if not snapshot.get("recorded", True):
        verdict += "; inferred from the staged row count (snapshot staged before population records)"
    detail = f"{method}{' ' + str(params) if params else ''}: {size}; {verdict}"
    return {"check": CHECK, "passed": passed, "method": method, "sampling": params, "truncated": truncated,
            "detail": detail}


@dataclass(frozen=True)
class Population:
    mode: str  # staged | pushdown | unknown
    snapshot: dict[str, Any] | None

    def caveat(self) -> str | None:
        return population_caveat(self.snapshot) if self.mode == "staged" else None

    def check(self) -> dict[str, Any]:
        return population_check(self.mode, self.snapshot)


def population_for(asset_fq: str | None, source_id: str | None) -> Population:
    """The population an analysed asset (``schema.table`` as the gateway sees it) describes.

    Snapshots staged before population records existed are judged from their row count: one that
    reached the cap is treated as an undeclared truncation."""
    from analystos.db.base import session_scope
    from analystos.db.models import Source, SourceAsset
    from analystos.services.platform_settings import get as platform

    if not asset_fq or not source_id:
        return Population("unknown", None)
    schema, _, name = asset_fq.partition(".")
    with session_scope() as s:
        src = s.get(Source, source_id)
        if src is None:
            return Population("unknown", None)
        if src.execution_mode != "staged":
            return Population("pushdown", None)
        row = s.scalar(select(SourceAsset).where(SourceAsset.source_id == source_id, SourceAsset.schema_name == schema,
                                                 SourceAsset.name == name))
        if row is None:
            return Population("staged", None)
        if row.snapshot:
            return Population("staged", dict(row.snapshot))
        rows, config, kind = row.row_count, dict(src.config or {}), src.kind
    if rows is None:
        return Population("staged", None)
    cap = sampling.row_cap(config, platform().sources.staged_max_rows, None)
    if kind == "servicenow":  # the connector's own default cap
        cap = min(cap, int(config.get("max_rows") or 200_000))
    snap = sampling.snapshot_record(spec=None, rows_staged=rows, cap=cap, truncated=rows >= cap, source_total_rows=None,
                                    total_basis="unavailable")
    return Population("staged", {**snap, "recorded": False})
