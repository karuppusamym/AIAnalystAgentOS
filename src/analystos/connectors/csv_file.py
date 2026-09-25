"""File connector (staged): CSV, Parquet and (when an Excel engine is installed) Excel.

``config.path`` is a file or a directory **inside the allowed upload directory**; anything that
resolves outside it (``..``, absolute paths elsewhere, symlinks) is refused. Types are inferred
with polars. Asset and column names are sanitized to ``[a-z0-9_]`` so what discovery reports is
exactly what the staging loader creates.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl
import pyarrow as pa

from analystos.connectors.base import ConnectionTest, DiscoveredAsset, DiscoveredColumn
from analystos.connectors.naming import sanitize_identifier, unique_identifiers
from analystos.core.config import REPO_ROOT
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.logging import get_logger

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
INFER_ROWS = 10_000

_log = get_logger(__name__)


def default_upload_dir(settings: Any | None = None) -> Path:
    configured = getattr(settings, "upload_dir", None) if settings is not None else None
    return Path(configured) if configured else REPO_ROOT / "var" / "uploads"


def excel_engine_available() -> bool:
    for module in ("fastexcel", "openpyxl"):
        try:
            __import__(module)
            return True
        except ImportError:
            continue
    return False


def normalize_polars_dtype(dtype: pl.DataType) -> str:
    if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.UInt8, pl.UInt16):
        return "integer"
    if dtype in (pl.Int64, pl.UInt32, pl.UInt64):
        return "bigint"
    if dtype in (pl.Float32, pl.Float64):
        return "double"
    if isinstance(dtype, pl.Decimal):
        return "numeric"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype == pl.Date:
        return "date"
    if isinstance(dtype, pl.Datetime):
        return "timestamp"
    if isinstance(dtype, (pl.List, pl.Struct, pl.Array)):
        return "json"
    return "text"


class CSVFileConnector:
    kind = "csv"
    execution_mode: Literal["pushdown", "staged"] = "staged"
    dialect = "postgres"

    def __init__(self, config: dict[str, Any], secret_ref: str | None = None, *, allowed_dir: Path | str | None = None) -> None:
        raw = config.get("path")
        if not raw:
            raise InvalidInput("File source needs config.path")
        self.allowed_dir = Path(allowed_dir or default_upload_dir()).resolve()
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = self.allowed_dir / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.allowed_dir):
            raise InvalidInput(f"File path must be inside the upload directory ({self.allowed_dir})")
        self.path = resolved
        self.delimiter = config.get("delimiter")
        self.sheet = config.get("sheet")

    # -- files -----------------------------------------------------------------------------

    def _files(self) -> list[Path]:
        if not self.path.exists():
            raise NotFound(f"File source path does not exist: {self.path.name}")
        if self.path.is_file():
            files = [self.path]
        else:
            files = sorted(p for p in self.path.iterdir() if p.is_file() and not p.name.startswith("."))
        out = []
        for f in files:
            if not f.resolve().is_relative_to(self.allowed_dir):
                continue  # symlink escaping the upload dir
            suffix = f.suffix.lower()
            if suffix in CSV_SUFFIXES | PARQUET_SUFFIXES:
                out.append(f)
            elif suffix in EXCEL_SUFFIXES:
                if excel_engine_available():
                    out.append(f)
                else:
                    _log.warning("skipping %s: no Excel engine installed (install openpyxl or fastexcel)", f.name)
        return out

    def _scan(self, file: Path) -> pl.LazyFrame:
        suffix = file.suffix.lower()
        if suffix in PARQUET_SUFFIXES:
            return pl.scan_parquet(file)
        if suffix in EXCEL_SUFFIXES:
            kwargs: dict[str, Any] = {}
            if self.sheet:
                kwargs["sheet_name"] = self.sheet
            return pl.read_excel(file, **kwargs).lazy()
        sep = self.delimiter or ("\t" if suffix == ".tsv" else ",")
        return pl.scan_csv(file, separator=sep, infer_schema_length=INFER_ROWS, try_parse_dates=True, ignore_errors=False)

    def _asset_names(self, files: list[Path]) -> dict[str, Path]:
        names = unique_identifiers([sanitize_identifier(f.stem, fallback="file") for f in files])
        return dict(zip(names, files, strict=True))

    # -- protocol --------------------------------------------------------------------------

    def test(self) -> ConnectionTest:
        started = time.perf_counter()
        try:
            files = self._files()
        except (NotFound, InvalidInput) as exc:
            return ConnectionTest(ok=False, message=exc.message)
        return ConnectionTest(
            ok=bool(files),
            message=f"{len(files)} readable file(s)" if files else "No supported files found",
            latency_ms=int((time.perf_counter() - started) * 1000),
            details={"files": [f.name for f in files]},
        )

    def discover(self) -> list[DiscoveredAsset]:
        assets = []
        for name, file in self._asset_names(self._files()).items():
            try:
                lf = self._scan(file)
                schema = lf.collect_schema()
                row_count = int(lf.select(pl.len()).collect().item())
            except Exception as exc:  # noqa: BLE001
                raise InvalidInput(f"Could not read {file.name}: {str(exc).splitlines()[0]}") from None
            col_names = unique_identifiers(list(schema.names()))
            columns = [
                DiscoveredColumn(
                    name=clean,
                    data_type=normalize_polars_dtype(dtype),
                    nullable=True,
                    business_name=original if original != clean else None,
                    description=f"From column '{original}' of {file.name}" if original != clean else None,
                )
                for clean, (original, dtype) in zip(col_names, schema.items(), strict=True)
            ]
            assets.append(
                DiscoveredAsset(
                    source_name=file.name,
                    name=name,
                    kind="file",
                    row_count=row_count,
                    description=f"Uploaded file {file.name}",
                    freshness_at=datetime.fromtimestamp(file.stat().st_mtime, tz=UTC),
                    columns=columns,
                )
            )
        return assets

    def extract(self, asset: DiscoveredAsset, *, max_rows: int) -> Iterator[pa.RecordBatch]:
        files = {f.name: f for f in self._files()}
        file = files.get(asset.source_name)
        if file is None:
            raise NotFound(f"File {asset.source_name} is no longer available")
        lf = self._scan(file)
        schema = lf.collect_schema()
        clean = unique_identifiers(list(schema.names()))
        exprs = []
        for original, name, dtype in zip(schema.names(), clean, schema.dtypes(), strict=True):
            e = pl.col(original)
            normalized = normalize_polars_dtype(dtype)
            if normalized == "text" and dtype != pl.String:
                e = e.cast(pl.String)
            exprs.append(e.alias(name))
        df = lf.select(exprs).head(max_rows).collect()
        wanted = [c.name for c in asset.columns] or clean
        missing = [c for c in wanted if c not in df.columns]
        if missing:
            raise InvalidInput(f"File {file.name} no longer has columns {missing}; rediscover the source")
        table = df.select(wanted).to_arrow()
        yield from table.to_batches(max_chunksize=50_000)

    def sqlalchemy_url(self) -> str:
        raise InvalidInput("File sources are staged; they have no SQL endpoint")
