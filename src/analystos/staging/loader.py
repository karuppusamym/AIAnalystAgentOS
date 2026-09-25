"""Staging loader: writes bounded snapshots of staged sources into the analytics database.

Only the *loader* identity (``settings.analytics_loader_url``) writes here. Each source gets its
own schema ``src_<source_id>``; each asset is loaded into ``<name>__load`` with COPY and then
swapped in atomically (drop old + rename, same transaction), after which the reader identity is
granted USAGE on the schema and SELECT on its tables. Identifiers are sanitized to
``[a-z0-9_]`` and always quoted.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
from psycopg import sql
from sqlalchemy.engine import make_url

from analystos.connectors.base import DiscoveredAsset
from analystos.connectors.naming import is_safe_identifier, sanitize_identifier, staging_schema_for, unique_identifiers
from analystos.core.errors import InvalidInput
from analystos.core.logging import get_logger
from analystos.gateway.engines import get_engine

LOAD_SUFFIX = "__load"
MAX_TABLE_NAME = 63 - len(LOAD_SUFFIX)

_log = get_logger(__name__)


def pg_type_for(dtype: pa.DataType) -> str:
    """Map an Arrow type to the Postgres column type used for staging."""
    t = pa.types
    if t.is_boolean(dtype):
        return "boolean"
    if t.is_int8(dtype) or t.is_int16(dtype) or t.is_uint8(dtype):
        return "smallint"
    if t.is_int32(dtype) or t.is_uint16(dtype):
        return "integer"
    if t.is_int64(dtype) or t.is_uint32(dtype):
        return "bigint"
    if t.is_uint64(dtype):
        return "numeric(20,0)"
    if t.is_float16(dtype) or t.is_float32(dtype):
        return "real"
    if t.is_float64(dtype):
        return "double precision"
    if t.is_decimal(dtype):
        return f"numeric({dtype.precision},{dtype.scale})"
    if t.is_string(dtype) or t.is_large_string(dtype) or (hasattr(t, "is_string_view") and t.is_string_view(dtype)):
        return "text"
    if t.is_date(dtype):
        return "date"
    if t.is_timestamp(dtype):
        return "timestamptz" if dtype.tz else "timestamp"
    if t.is_time(dtype):
        return "time"
    if t.is_duration(dtype):
        return "interval"
    if t.is_binary(dtype) or t.is_large_binary(dtype) or t.is_fixed_size_binary(dtype):
        return "bytea"
    if t.is_list(dtype) or t.is_large_list(dtype) or t.is_struct(dtype) or t.is_map(dtype) or t.is_fixed_size_list(dtype):
        return "jsonb"
    if t.is_dictionary(dtype):
        return pg_type_for(dtype.value_type)
    if t.is_null(dtype):
        return "text"
    return "text"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _row_converter(pg_types: list[str]):  # noqa: ANN202
    json_cols = [i for i, ty in enumerate(pg_types) if ty == "jsonb"]
    text_cols = [i for i, ty in enumerate(pg_types) if ty == "text"]
    interval_cols = [i for i, ty in enumerate(pg_types) if ty == "interval"]

    def convert(row: list[Any]) -> list[Any]:
        for i in json_cols:
            if row[i] is not None:
                row[i] = json.dumps(row[i], default=_json_default)
        for i in text_cols:
            v = row[i]
            if v is not None and not isinstance(v, str):
                row[i] = str(v)
            if isinstance(row[i], str) and "\x00" in row[i]:
                row[i] = row[i].replace("\x00", "")
        for i in interval_cols:
            if row[i] is not None:
                row[i] = str(row[i])
        return row

    return convert


class StagingLoader:
    def __init__(self, settings: Any, *, loader_url: str | None = None, reader_role: str | None = None) -> None:
        self.settings = settings
        self.loader_url = loader_url or settings.analytics_loader_url
        reader_url = getattr(settings, "analytics_reader_url", None)
        self.reader_role = reader_role or (make_url(reader_url).username if reader_url else "analystos_reader")
        if not self.reader_role or not is_safe_identifier(self.reader_role):
            raise InvalidInput("analytics reader role name is not a safe identifier")

    def _engine(self):  # noqa: ANN202
        return get_engine(self.loader_url)

    def load(self, source_id: str, asset: DiscoveredAsset | str, batches: Iterable[pa.RecordBatch], *,
             snapshot: Callable[[], dict[str, Any] | None] | None = None) -> dict[str, Any]:
        """Load ``batches`` as ``src_<source_id>.<asset name>`` and return
        ``{"row_count", "schema", "table", "columns": [{"name", "type"}]}``, plus ``snapshot`` and
        ``truncated`` when ``snapshot`` (read after the batches are exhausted) describes the population."""
        schema_name = staging_schema_for(source_id)
        raw_name = asset.name if isinstance(asset, DiscoveredAsset) else str(asset)
        table_name = sanitize_identifier(raw_name, max_length=MAX_TABLE_NAME, fallback="t")
        load_name = f"{table_name}{LOAD_SUFFIX}"
        for ident in (schema_name, table_name, load_name):
            if not is_safe_identifier(ident):
                raise InvalidInput(f"Unsafe identifier {ident!r}")

        schema_ident = sql.Identifier(schema_name)
        load_ident = sql.Identifier(schema_name, load_name)
        final_ident = sql.Identifier(schema_name, table_name)
        reader_ident = sql.Identifier(self.reader_role)

        iterator = iter(batches)
        first = next(iterator, None)
        if first is None:
            if isinstance(asset, DiscoveredAsset) and asset.columns:
                from analystos.connectors.servicenow import arrow_type  # normalized -> arrow

                arrow_schema = pa.schema([pa.field(c.name, arrow_type(c.data_type)) for c in asset.columns])
            else:
                raise InvalidInput(f"No data and no column metadata for {raw_name}; nothing to stage")
        else:
            arrow_schema = first.schema
        col_names = unique_identifiers(arrow_schema.names)
        pg_types = [pg_type_for(f.type) for f in arrow_schema]
        columns_sql = sql.SQL(", ").join(
            sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(ty)) for n, ty in zip(col_names, pg_types, strict=True)
        )
        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
            load_ident, sql.SQL(", ").join(sql.Identifier(n) for n in col_names)
        )
        convert = _row_converter(pg_types)

        raw = self._engine().raw_connection()
        try:
            conn = raw.driver_connection
            row_count = 0
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema_ident))
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(load_ident))
                cur.execute(sql.SQL("CREATE TABLE {} ({})").format(load_ident, columns_sql))
                with cur.copy(copy_sql) as copy:
                    batch_iter = [first] if first is not None else []
                    for batch in _chain(batch_iter, iterator):
                        if batch.num_rows == 0:
                            continue
                        if batch.schema.names != arrow_schema.names:
                            raise InvalidInput(f"Batch schema changed while loading {raw_name}")
                        cols = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
                        for values in zip(*cols, strict=True):
                            copy.write_row(convert(list(values)))
                        row_count += batch.num_rows
                # Atomic swap: readers see either the old snapshot or the new one.
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(final_ident))
                cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(load_ident, sql.Identifier(table_name)))
                cur.execute(sql.SQL("ANALYZE {}").format(final_ident))
                cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, reader_ident))
                cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(schema_ident, reader_ident))
            conn.commit()
        except Exception:
            try:
                raw.driver_connection.rollback()
            finally:
                raw.close()
            raise
        raw.close()
        _log.info("staged %s rows into %s.%s", row_count, schema_name, table_name)
        info: dict[str, Any] = {
            "row_count": row_count,
            "schema": schema_name,
            "table": table_name,
            "columns": [{"name": n, "type": ty} for n, ty in zip(col_names, pg_types, strict=True)],
        }
        record = snapshot() if snapshot is not None else None
        if record:
            info["snapshot"] = {**record, "rows_staged": row_count}
            info["truncated"] = bool(record.get("truncated"))
        return info

    def drop_source(self, source_id: str) -> None:
        schema_name = staging_schema_for(source_id)
        if not is_safe_identifier(schema_name):
            raise InvalidInput(f"Unsafe identifier {schema_name!r}")
        raw = self._engine().raw_connection()
        try:
            conn = raw.driver_connection
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
            conn.commit()
        except Exception:
            raw.driver_connection.rollback()
            raise
        finally:
            raw.close()


def _chain(first: list[pa.RecordBatch], rest: Iterable[pa.RecordBatch]):  # noqa: ANN202
    yield from first
    yield from rest
