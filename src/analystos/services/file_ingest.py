"""File ingestion (P6-06): CSV, JSON (arrays and NDJSON), Excel and Parquet into a staged file
source, through a mapping contract and the existing Arrow + COPY loader.

The mapping contract is checked before anything is written: every mapped source column exists in the
file, target names are unique after sanitising (`Order ID` and `order_id` may not both land in
`order_id`), a declared type converts every value or the load is refused with the column and row
named (DataPilot's loader turned a bad value into NULL silently; its load modes and mapping checks are
the idea kept, AienginnerAgentOs@15235dc:apps/api/app/staging.py). Load modes are the loader's:
replace, append, merge on keys (idempotent on retry). The staged table keeps the per-workspace
grants and gets a content fingerprint computed from the table itself, so re-ingesting the same content
in any mode keeps its data version and changed content gets a new one (P4-03).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.connectors.naming import sanitize_identifier
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn, User
from analystos.governance.policy import load_in_workspace, scoped_loader

FORMATS = ("csv", "json", "excel", "parquet")
FILE_KINDS = ("csv",)  # the staged file source kind (config/source_kinds.yaml `csv`, alias `file`)
_SUFFIX_FORMAT = {".csv": "csv", ".tsv": "csv", ".txt": "csv", ".json": "json", ".ndjson": "json", ".jsonl": "json",
                  ".xlsx": "excel", ".xlsm": "excel", ".xls": "excel", ".parquet": "parquet", ".pq": "parquet"}
_TYPES = {"text": pl.String, "integer": pl.Int32, "bigint": pl.Int64, "smallint": pl.Int16, "double": pl.Float64,
          "boolean": pl.Boolean, "date": pl.Date, "timestamp": pl.Datetime("us")}


class ColumnMapping(BaseModel):
    source: str
    target: str | None = None  # default: the source name, sanitised
    type: str | None = None  # text, smallint, integer, bigint, double, numeric(p,s), boolean, date, timestamp


class IngestSpec(BaseModel):
    path: str  # inside the workspace's upload folder (POST /workspaces/{id}/uploads), relative or absolute
    table: str
    format: Literal["csv", "json", "excel", "parquet"] | None = None  # default: from the file suffix
    sheet: str | None = None
    delimiter: str | None = None
    mode: Literal["replace", "append", "merge"] = "replace"
    keys: list[str] = Field(default_factory=list)  # merge keys, as target names
    mapping: list[ColumnMapping] | None = None  # default: every column, names sanitised


def detect_format(path: Path, declared: str | None = None) -> str:
    fmt = declared or _SUFFIX_FORMAT.get(path.suffix.lower())
    if fmt not in FORMATS:
        raise InvalidInput(f"cannot tell the format of {path.name}; pass format (one of {', '.join(FORMATS)})")
    return fmt


def read_file(path: Path, fmt: str, *, sheet: str | None = None, delimiter: str | None = None) -> pl.DataFrame:
    """The whole file as a DataFrame. Types are inferred over the whole file (CSV) and a value that does
    not fit the inferred type is an error, never a NULL."""
    try:
        if fmt == "parquet":
            return pl.read_parquet(path)
        if fmt == "excel":
            from analystos.connectors.csv_file import excel_engine

            engine = excel_engine()
            if engine is None:
                raise InvalidInput("no Excel reader is installed (openpyxl or fastexcel)")
            kwargs: dict[str, Any] = {"sheet_name": sheet} if sheet else {}
            return pl.read_excel(path, engine=engine, **kwargs)
        if fmt == "json":
            head = path.read_bytes()[:4096].lstrip()
            if head[:1] == b"[":
                return pl.read_json(path)
            if head[:1] == b"{":
                return pl.read_ndjson(path)
            raise InvalidInput(f"{path.name} is neither a JSON array of objects nor newline-delimited JSON objects")
        sep = delimiter or ("\t" if path.suffix.lower() == ".tsv" else ",")
        return pl.read_csv(path, separator=sep, infer_schema_length=None, try_parse_dates=True)
    except InvalidInput:
        raise
    except Exception as exc:  # noqa: BLE001 - reader-specific errors
        raise InvalidInput(f"could not read {path.name} as {fmt}: {str(exc).splitlines()[0][:300]}") from None


def _cast(series: pl.Series, ctype: str, source: str) -> pl.Series:
    from analystos.contracts.recipe import canonical_type

    target = canonical_type(ctype)
    if target is None:
        raise InvalidInput(f"mapping for {source!r}: unsupported type {ctype!r}")
    if target.startswith("numeric"):
        params = target[len("numeric("):-1].split(",") if "(" in target else ["38", "9"]
        dtype: Any = pl.Decimal(int(params[0]), int(params[1]))
    elif target == "timestamptz":
        dtype = pl.Datetime("us", "UTC")
    else:
        dtype = _TYPES[target]
    def convert(s: pl.Series) -> pl.Series:
        if s.dtype == pl.String and dtype == pl.Date:
            return s.str.strip_chars().str.to_date(strict=True)
        if s.dtype == pl.String and isinstance(dtype, pl.Datetime):
            return s.str.strip_chars().str.to_datetime(strict=True, time_unit="us")
        if s.dtype == pl.String and dtype == pl.Boolean:
            mapped = s.str.strip_chars().str.to_lowercase().replace_strict(
                {"true": True, "t": True, "yes": True, "1": True, "false": False, "f": False, "no": False, "0": False},
                default=None, return_dtype=pl.Boolean)
            if (mapped.is_null() & s.is_not_null()).any():
                raise pl.exceptions.InvalidOperationError("not a boolean")
            return mapped
        return s.cast(dtype, strict=True)

    try:
        return convert(series)
    except (pl.exceptions.PolarsError, ValueError):
        bad = None
        for i in range(series.len()):
            one = series.slice(i, 1)
            if one.null_count():
                continue
            try:
                convert(one)
            except (pl.exceptions.PolarsError, ValueError):
                bad = i + 1
                break
        where = f" (first at row {bad})" if bad else ""
        raise InvalidInput(f"column {source!r} does not convert to {target}{where}; fix the file or map it as text",
                           details={"column": source, "row": bad}) from None


def apply_mapping(df: pl.DataFrame, mapping: list[ColumnMapping] | None) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """The mapped frame and the contract as applied: [{source, target, type}]."""
    entries = mapping if mapping is not None else [ColumnMapping(source=c) for c in df.columns]
    if not entries:
        raise InvalidInput("the mapping selects no column")
    missing = [e.source for e in entries if e.source not in df.columns]
    if missing:
        raise InvalidInput(f"mapped column {', '.join(repr(m) for m in missing)} is not in the file "
                           f"(columns: {', '.join(df.columns)})", details={"columns": missing})
    targets: dict[str, str] = {}
    out: list[pl.Series] = []
    applied: list[dict[str, Any]] = []
    for e in entries:
        target = sanitize_identifier(e.target or e.source, fallback="col")
        if target in targets:
            raise InvalidInput(f"columns {targets[target]!r} and {e.source!r} both become {target!r} after sanitising; "
                               "map one of them to another name", details={"columns": [targets[target], e.source]})
        targets[target] = e.source
        series = df.get_column(e.source)
        if e.type:
            series = _cast(series, e.type, e.source)
        out.append(series.alias(target))
        applied.append({"source": e.source, "target": target, "type": e.type or str(series.dtype)})
    return pl.DataFrame(out), applied


@scoped_loader
def _file_source(session: Session, user: User, source_id: str, workspace_id: str | None = None) -> Source:
    src = load_in_workspace(session, Source, source_id, workspace_id, user=user, minimum="editor", label="source")
    if src.kind not in FILE_KINDS:
        raise InvalidInput(f"source {source_id} is a {src.kind} source; files are ingested into a file source (kind csv)")
    return src


@scoped_loader
def ingest_file(user: User, source_id: str, spec: IngestSpec, workspace_id: str | None = None) -> dict[str, Any]:
    """Map and load one uploaded file into the file source's staged schema; the table becomes a
    selected asset of the source (readable through the gateway by the workspace)."""
    from analystos.connectors.csv_file import CSVFileConnector, default_upload_dir
    from analystos.core.config import get_settings
    from analystos.events.bus import emit
    from analystos.evidence.manifest import mark_stale
    from analystos.governance.audit import audit
    from analystos.staging.loader import MAX_TABLE_NAME, StagingLoader

    settings = get_settings()
    with session_scope() as s:
        src = _file_source(s, user, source_id, workspace_id=workspace_id)
        ws, staging_schema = src.workspace_id, src.staging_schema
    table = sanitize_identifier(spec.table, max_length=MAX_TABLE_NAME, fallback="t")
    # Only the workspace's own upload folder (where POST /workspaces/{id}/uploads writes).
    connector = CSVFileConnector({"path": spec.path}, allowed_dir=Path(default_upload_dir(settings)) / ws)
    path = connector.path
    if not path.is_file():
        raise NotFound(f"file {path.name} is not in the upload directory")
    fmt = detect_format(path, spec.format)
    df, applied = apply_mapping(read_file(path, fmt, sheet=spec.sheet, delimiter=spec.delimiter), spec.mapping)
    unknown_keys = [k for k in spec.keys if k not in df.columns]
    if unknown_keys:
        raise InvalidInput(f"merge key {', '.join(unknown_keys)} is not a mapped target column "
                           f"(targets: {', '.join(df.columns)})", details={"columns": unknown_keys})
    if spec.mode == "merge" and not spec.keys:
        raise InvalidInput("merge needs keys (target column names)")
    null_keys = [k for k in spec.keys if df.get_column(k).null_count()] if spec.mode == "merge" else []
    if null_keys:
        k = null_keys[0]
        row = int(df.with_row_index("aos_row").filter(pl.col(k).is_null()).get_column("aos_row")[0]) + 1
        raise InvalidInput(f"merge key column {k} is null in row {row} of {path.name}; a merge needs every key",
                           details={"column": k, "row": row})
    info = StagingLoader(settings).load(source_id, table, df.to_arrow().to_batches(max_chunksize=50_000),
                                        workspace_id=ws, mode=spec.mode, keys=spec.keys, fingerprint="table")
    snapshot = {"sampling_method": "full", "rows_staged": info["row_count"], "truncated": False, "recorded": True,
                "source_total_rows": info["row_count"], "total_rows_basis": "count", "row_cap": None,
                "staged_at": utcnow().isoformat(), "load_id": new_id("load"),
                "content_fingerprint": info["content_fingerprint"],
                "ingest": {"file": path.name, "format": fmt, "mode": spec.mode, "keys": spec.keys,
                           "rows_loaded": info.get("rows_loaded"), "replaced_rows": info.get("replaced_rows"),
                           "mapping": applied}}
    with session_scope() as s:
        row = s.get(Source, source_id)
        schema_name = info["schema"]
        asset = s.scalar(select(SourceAsset).where(SourceAsset.source_id == source_id, SourceAsset.schema_name == schema_name,
                                                   SourceAsset.name == table))
        previous = (asset.snapshot or {}).get("content_fingerprint") if asset is not None else None
        if asset is None:
            asset = SourceAsset(id=new_id("ast"), source_id=source_id, workspace_id=ws, schema_name=schema_name, name=table,
                                source_name=path.name, kind="file", selected=True, description=f"Ingested from {path.name}")
            s.add(asset)
            s.flush()
        asset.selected, asset.row_count, asset.freshness_at, asset.snapshot = True, info["row_count"], utcnow(), snapshot
        asset.lifecycle = "active"
        existing = {c.name: c for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id))}
        wanted = [c["name"] for c in info["columns"]]
        for name, col in existing.items():
            if name not in wanted:
                s.delete(col)
        by_target = {m["target"]: m["source"] for m in applied}
        for i, c in enumerate(info["columns"]):
            col = existing.get(c["name"])
            if col is None:
                col = SourceColumn(asset_id=asset.id, name=c["name"], tags=[], profile={}, semantics={})
                s.add(col)
            col.ordinal, col.data_type, col.nullable = i, c["type"], True
            col.is_key = c["name"] in spec.keys
            if by_target.get(c["name"]) not in (None, c["name"]) and not col.business_name:
                col.business_name = by_target[c["name"]]
        row.status, row.last_discovered_at = "ready", utcnow()
        row.staging_schema = row.staging_schema or staging_schema or schema_name
        s.flush()
        if previous and previous != info["content_fingerprint"]:
            mark_stale(s, ws, source_id, f"{schema_name}.{table}")
        result = {"asset": f"{schema_name}.{table}", "asset_id": asset.id, "mode": spec.mode, "format": fmt,
                  "row_count": info["row_count"], "rows_loaded": info.get("rows_loaded"),
                  "replaced_rows": info.get("replaced_rows"), "content_fingerprint": info["content_fingerprint"],
                  "changed": previous != info["content_fingerprint"], "columns": info["columns"], "mapping": applied}
        audit(f"user:{user.id}", "file.ingested", workspace_id=ws, target=source_id,
              details={k: result[k] for k in ("asset", "mode", "format", "row_count", "rows_loaded", "changed")}, session=s)
        emit(ws, "file.ingested", {k: result[k] for k in ("asset", "mode", "row_count", "rows_loaded", "changed")},
             actor=f"user:{user.id}", session=s)
    return result
