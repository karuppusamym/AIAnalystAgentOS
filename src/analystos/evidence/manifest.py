"""Data-version manifest (P4-03, ADR-0011 "Reproducibility and data lifecycle").

A run records which version of each analysed asset it read; every finding references that manifest.
The version of a staged asset is its **content**: the loader's order-independent row fingerprint, the
row count and the sampling method (so re-staging identical data keeps the version, and any changed,
added or removed row changes it). Snapshots staged before content fingerprints existed are versioned by
their load metadata (load id / staged time / rows), which changes on every re-stage. A pushdown source
has no fixed version: the entry records when the run observed it and the finding is best-effort replay.

When a snapshot's version changes, findings bound to the old version are marked **stale** (they keep
their evidence; they need re-verification before promotion) and their verification records turn
``VOID(data)`` (P7-01, `evidence.verification`). A changed snapshot is not a failed method.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.evidence import DataManifest, Freshness, ManifestEntry
from analystos.core.ids import stable_hash, utcnow


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, datetime) else str(v)


def entry_from(asset: str, source_id: str | None, mode: str, *, snapshot: Mapping[str, Any] | None = None,
               row_count: int | None = None, freshness_at: Any = None, structure: str | None = None,
               observed_at: str | None = None) -> ManifestEntry:
    """One manifest entry from what the platform recorded about the asset (pure; no data access)."""
    snap = dict(snapshot or {})
    sampling = snap.get("sampling") or {}
    if mode != "staged":
        return ManifestEntry(asset=asset, source_id=source_id, mode="pushdown" if mode == "pushdown" else "unknown",
                             rows=row_count, structure_fingerprint=structure, observed_at=observed_at or utcnow().isoformat(),
                             immutable=False, version=None, version_basis="none")
    rows = snap.get("rows_staged", row_count)
    content = snap.get("content_fingerprint")
    staged_at = snap.get("staged_at") or _iso(freshness_at)
    if content:
        basis, ident = "content", {"asset": asset, "source": source_id, "content": content, "rows": rows,
                                   "sampling": snap.get("sampling_method")}
    elif snap.get("load_id") or staged_at or rows is not None:
        basis, ident = "load_metadata", {"asset": asset, "source": source_id, "load": snap.get("load_id"),
                                         "staged_at": staged_at, "rows": rows}
    else:
        basis, ident = "none", None
    return ManifestEntry(
        asset=asset, source_id=source_id, mode="staged", load_id=snap.get("load_id"),
        rows=None if rows is None else int(rows), source_total_rows=snap.get("source_total_rows"),
        content_fingerprint=content, structure_fingerprint=structure, sampling_method=snap.get("sampling_method"),
        window_start=_iso(sampling.get("window_start")), window_end=_iso(sampling.get("window_end")),
        staged_at=staged_at, observed_at=observed_at, immutable=ident is not None,
        version=stable_hash(ident) if ident is not None else None, version_basis=basis)  # type: ignore[arg-type]


def build(entries: Iterable[ManifestEntry], *, built_at: str | None = None) -> DataManifest:
    ordered = sorted(entries, key=lambda e: e.asset)
    version = stable_hash([[e.asset, e.version or f"unversioned:{e.mode}"] for e in ordered])
    return DataManifest(version=version, built_at=built_at or utcnow().isoformat(), entries=ordered)


def current_entry(session: Session, asset: str, source_id: str | None) -> ManifestEntry:
    """The asset's current version, from its source and snapshot records."""
    from analystos.db.models import Source, SourceAsset

    src = session.get(Source, source_id) if source_id else None
    if src is None:
        return entry_from(asset, source_id, "unknown")
    if src.execution_mode != "staged":
        return entry_from(asset, source_id, "pushdown")
    schema, _, name = asset.partition(".")
    row = session.scalar(select(SourceAsset).where(SourceAsset.source_id == source_id, SourceAsset.schema_name == schema,
                                                   SourceAsset.name == name))
    if row is None:
        return entry_from(asset, source_id, "staged")
    return entry_from(asset, source_id, "staged", snapshot=row.snapshot, row_count=row.row_count,
                      freshness_at=row.freshness_at, structure=row.fingerprint)


def manifest_for(session: Session, asset_sources: Mapping[str, str | None], assets: Iterable[str]) -> DataManifest:
    now = utcnow().isoformat()
    entries = []
    for asset in sorted(set(assets)):
        e = current_entry(session, asset, asset_sources.get(asset))
        if e.mode == "pushdown":
            e.observed_at = now
        entries.append(e)
    return build(entries, built_at=now)


def ensure_run_manifest(run_id: str, asset_sources: Mapping[str, str | None], assets: Iterable[str]) -> DataManifest:
    """The run's manifest, recorded once (the first time findings are drafted) and reused after."""
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        if run is not None and run.data_manifest:
            return DataManifest.model_validate(run.data_manifest)
        manifest = manifest_for(s, asset_sources, assets)
        if run is not None:
            run.data_manifest = manifest.model_dump(mode="json")
        return manifest


def changed(recorded: ManifestEntry | Mapping[str, Any], current: ManifestEntry) -> str | None:
    """Why `current` is a different data version from `recorded` (None: same version, or unknowable)."""
    rec = recorded if isinstance(recorded, ManifestEntry) else ManifestEntry.model_validate(recorded)
    if rec.mode != "staged" or current.mode != "staged":
        return None
    if rec.version and current.version and rec.version != current.version:
        what = []
        if rec.rows != current.rows:
            what.append(f"rows {rec.rows} -> {current.rows}")
        if rec.content_fingerprint != current.content_fingerprint and rec.content_fingerprint and current.content_fingerprint:
            what.append("row content changed")
        if rec.load_id != current.load_id and current.version_basis == "load_metadata":
            what.append("re-staged")
        return f"snapshot of {current.asset} changed ({'; '.join(what) or 'new version'})"
    return None


def freshness(bundle: Mapping[str, Any], current: Mapping[str, ManifestEntry]) -> Freshness:
    """Whether a finding's recorded data versions are still current (pure)."""
    entries = ((bundle.get("data") or {}).get("manifest") or {}).get("entries") or []
    if not entries:
        return Freshness(state="unknown", reason="no data-version manifest recorded")
    reasons, assets = [], []
    for e in entries:
        now = current.get(e.get("asset"))
        why = changed(e, now) if now is not None else None
        if why:
            reasons.append(why)
            assets.append(e.get("asset"))
    if reasons:
        return Freshness(state="stale", since=utcnow().isoformat(), reason="; ".join(reasons), assets=assets)
    return Freshness(state="current")


def mark_stale(session: Session, workspace_id: str, source_id: str, asset: str) -> list[str]:
    """After `asset` was re-staged: mark findings bound to an older version stale (needs re-verification).
    Their evidence is kept; `stale_since` and the bundle's `freshness` say why. Returns the insight ids."""
    from analystos.db.models import Insight
    from analystos.events.bus import emit

    now_entry = current_entry(session, asset, source_id)
    marked = []
    for ins in session.scalars(select(Insight).where(Insight.workspace_id == workspace_id, Insight.stale_since.is_(None),
                                                     Insight.data_version.is_not(None), Insight.status != "superseded")):
        bundle = dict(ins.evidence_bundle or {})
        state = freshness(bundle, {asset: now_entry})
        if state.state != "stale":
            continue
        ins.stale_since = utcnow()
        bundle["freshness"] = state.model_dump(mode="json")
        ins.evidence_bundle = bundle
        marked.append(ins.id)
        emit(workspace_id, "insight.stale", {"code": ins.code, "insight_id": ins.id, "asset": asset, "reason": state.reason},
             run_id=ins.run_id, session=session)
    # P7-01: `stale` is the data case of VOID. Every verdict that read an older version of this snapshot
    # (in any workspace sharing the source) is voided in this transaction.
    from analystos.evidence.verification import dependency_changed

    dependency_changed(session, "data", f"{source_id}/{asset}",
                       f"data snapshot {asset} changed (now version {(now_entry.version or 'unversioned')[:12]})",
                       event="snapshot.version_changed")
    return marked
