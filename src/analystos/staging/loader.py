"""Staging loader: writes bounded snapshots of staged sources into the analytics database.

Only the *loader* identity (``settings.analytics_loader_url``) writes here. Each source gets its
own schema ``src_<source_id>``; each asset is loaded into ``<name>__load`` with COPY and then
swapped in atomically (drop old + rename, same transaction), after which the owning workspace's
reader role (``staging/roles.py``) is granted USAGE on the schema and SELECT on its tables; the
reader login itself holds no direct grant. Identifiers are sanitized to ``[a-z0-9_]`` and always
quoted.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
from psycopg import sql

from analystos.connectors.base import DiscoveredAsset
from analystos.connectors.naming import is_safe_identifier, sanitize_identifier, staging_schema_for, unique_identifiers
from analystos.core.errors import InvalidInput
from analystos.core.logging import get_logger
from analystos.gateway.engines import get_engine
from analystos.staging.roles import ensure_workspace_role, grant_schema, reader_login, role_for

LOAD_SUFFIX = "__load"
LOAD_MODES = ("replace", "append", "merge")
MAX_TABLE_NAME = 63 - len(LOAD_SUFFIX)
# Per-schema load state (P6-02): the incremental watermark of each table, written in the same transaction as
# the rows it describes, so a crash can never leave the watermark ahead of the data. Loader-only: the
# workspace reader role is refused SELECT on it.
STATE_TABLE = "aos_load_state"
StateFn = Callable[[dict[str, Any] | None], dict[str, Any] | None]

_log = get_logger(__name__)


class ContentFingerprint:
    """Order-independent digest of the staged rows (P4-03 data-version manifest). A connector does not
    promise a row order, so the rows are hashed individually and summed modulo 2^64 (a multiset hash):
    re-staging identical data gives the same fingerprint, any changed, added or removed row changes it."""

    def __init__(self, columns: list[str], types: list[str]) -> None:
        self._acc = 0
        self._rows = 0
        self._head = json.dumps([columns, types])

    def add(self, row: list[Any]) -> None:
        digest = hashlib.blake2b(repr(row).encode(), digest_size=8).digest()
        self._acc = (self._acc + int.from_bytes(digest, "big")) % (1 << 64)
        self._rows += 1

    def hexdigest(self) -> str:
        return hashlib.sha256(f"{self._head}|{self._rows}|{self._acc:016x}".encode()).hexdigest()


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
        self.reader_role = reader_role or (reader_login(settings) if getattr(settings, "analytics_reader_url", None)
                                           else "analystos_reader")
        if not self.reader_role or not is_safe_identifier(self.reader_role):
            raise InvalidInput("analytics reader role name is not a safe identifier")

    def _engine(self):  # noqa: ANN202
        return get_engine(self.loader_url, plane="loader")

    def load(self, source_id: str, asset: DiscoveredAsset | str, batches: Iterable[pa.RecordBatch], *,
             workspace_id: str, snapshot: Callable[[], dict[str, Any] | None] | None = None, mode: str = "replace",
             keys: list[str] | None = None, fingerprint: str = "stream", state: StateFn | None = None) -> dict[str, Any]:
        """Load ``batches`` as ``src_<source_id>.<asset name>`` readable only by ``workspace_id``'s
        reader role and return ``{"row_count", "schema", "table", "columns": [{"name", "type"}]}``, plus
        ``snapshot`` and ``truncated`` when ``snapshot`` (read after the batches are exhausted) describes
        the population.

        ``mode`` (P6-06): ``replace`` swaps the table atomically (the default); ``append`` inserts the rows;
        ``merge`` replaces the rows whose ``keys`` match and inserts the rest, so re-applying the same batch
        (a retry) leaves the same table. Append and merge refuse a batch whose columns or types differ from
        the table's, a null key and a key repeated in the batch, naming the column; nothing is written then.
        ``fingerprint="table"`` (always for append/merge) hashes the resulting table in the database, so
        the fingerprint is a function of the table's content whatever mode produced it.

        Writers of one table are serialized (a transaction-scoped advisory lock), so overlapping loads
        apply one after the other. ``state`` (P6-02) receives the table's stored load state, read under that
        lock, and returns the new one (None keeps it); it is written in the same transaction as the rows,
        after them, so the watermark is committed only with the durable load."""
        if mode not in LOAD_MODES:
            raise InvalidInput(f"load mode must be one of {', '.join(LOAD_MODES)}")
        keys = list(keys or [])
        if mode == "merge" and not keys:
            raise InvalidInput("merge needs key columns")
        schema_name = staging_schema_for(source_id)
        ws_role = role_for(self.settings, workspace_id)
        raw_name = asset.name if isinstance(asset, DiscoveredAsset) else str(asset)
        table_name = sanitize_identifier(raw_name, max_length=MAX_TABLE_NAME, fallback="t")
        load_name = f"{table_name}{LOAD_SUFFIX}"
        for ident in (schema_name, table_name, load_name):
            if not is_safe_identifier(ident):
                raise InvalidInput(f"Unsafe identifier {ident!r}")
        if table_name == STATE_TABLE:
            raise InvalidInput(f"{STATE_TABLE} is reserved for the loader's own state")

        schema_ident = sql.Identifier(schema_name)
        load_ident = sql.Identifier(schema_name, load_name)
        final_ident = sql.Identifier(schema_name, table_name)

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
        content = ContentFingerprint(col_names, pg_types)
        missing_keys = [k for k in keys if k not in col_names]
        if missing_keys:
            raise InvalidInput(f"key column {', '.join(missing_keys)} is not a column of {raw_name} "
                               f"(columns: {', '.join(col_names)})")
        key_idx = [(col_names.index(k), k) for k in keys] if mode == "merge" else []

        raw = self._engine().raw_connection()
        merge_info: dict[str, Any] = {}
        try:
            conn = raw.driver_connection
            row_count = 0
            with conn.cursor() as cur:
                _lock(cur, schema_name, table_name)
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
                            converted = convert(list(values))
                            for i, name in key_idx:
                                if converted[i] is None:
                                    raise InvalidInput(f"merge key column {name} is null in row {row_count + 1} of "
                                                       f"{raw_name}; a merge needs every key", details={"column": name})
                            content.add(converted)
                            copy.write_row(converted)
                            row_count += 1
                cur.execute("SELECT to_regclass(%s)", (f'"{schema_name}"."{table_name}"',))
                exists = cur.fetchone()[0] is not None
                if mode == "merge":
                    merge_info["duplicate_keys"] = _duplicate_keys(cur, load_ident, keys)
                    if merge_info["duplicate_keys"]:
                        raise InvalidInput(f"merge key ({', '.join(keys)}) repeats in {merge_info['duplicate_keys']} key "
                                           f"value(s) of {raw_name}; a merge needs one row per key",
                                           details={"columns": keys})
                if mode != "replace" and exists:
                    _check_same_columns(cur, final_ident, load_ident, raw_name)
                    if mode == "merge":
                        on = sql.SQL(" AND ").join(sql.SQL("f.{k} = l.{k}").format(k=sql.Identifier(k)) for k in keys)
                        cur.execute(sql.SQL("DELETE FROM {} AS f USING {} AS l WHERE {}").format(final_ident, load_ident, on))
                        merge_info["replaced_rows"] = max(cur.rowcount or 0, 0)
                    names = sql.SQL(", ").join(sql.Identifier(n) for n in col_names)
                    cur.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(final_ident, names, names,
                                                                                       load_ident))
                    cur.execute(sql.SQL("DROP TABLE {}").format(load_ident))
                else:
                    # Atomic swap: readers see either the old snapshot or the new one.
                    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(final_ident))
                    cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(load_ident, sql.Identifier(table_name)))
                cur.execute(sql.SQL("ANALYZE {}").format(final_ident))
                if mode != "replace" or fingerprint == "table":
                    merge_info["fingerprint"], merge_info["table_rows"] = _table_fingerprint(cur, final_ident, col_names,
                                                                                             pg_types)
                if state is not None:
                    merge_info["state"] = _write_state(cur, schema_name, table_name, state)
                ensure_workspace_role(cur, ws_role, self.reader_role)
                grant_schema(cur, schema_name, ws_role, self.reader_role)
                _hide_state(cur, schema_name, ws_role)
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
            "row_count": merge_info.get("table_rows", row_count),
            "schema": schema_name,
            "table": table_name,
            "columns": [{"name": n, "type": ty} for n, ty in zip(col_names, pg_types, strict=True)],
            "content_fingerprint": merge_info.get("fingerprint") or content.hexdigest(),
        }
        if mode != "replace" or fingerprint == "table":
            info.update(mode=mode, rows_loaded=row_count, keys=keys, replaced_rows=merge_info.get("replaced_rows", 0),
                        fingerprint_basis="table")
        if state is not None:
            info["state"] = merge_info.get("state")
        record = snapshot() if snapshot is not None else None
        if record:
            info["snapshot"] = {**record, "rows_staged": row_count}
            info["truncated"] = bool(record.get("truncated"))
        return info

    # ------------------------------------------------------------------ load state (P6-02)
    def _names(self, source_id: str, table: str) -> tuple[str, str]:
        schema_name = staging_schema_for(source_id)
        table_name = sanitize_identifier(table, max_length=MAX_TABLE_NAME, fallback="t")
        for ident in (schema_name, table_name):
            if not is_safe_identifier(ident):
                raise InvalidInput(f"Unsafe identifier {ident!r}")
        return schema_name, table_name

    def read_state(self, source_id: str, table: str) -> dict[str, Any] | None:
        """The stored load state of a staged table (None when the table or its state does not exist). The
        analytics database is the authority: a control-plane copy may lag behind a crash, this cannot."""
        schema_name, table_name = self._names(source_id, table)
        raw = self._engine().raw_connection()
        try:
            with raw.driver_connection.cursor() as cur:
                cur.execute("SELECT to_regclass(%s), to_regclass(%s)", (f'"{schema_name}"."{STATE_TABLE}"',
                                                                        f'"{schema_name}"."{table_name}"'))
                state_exists, table_exists = cur.fetchone()
                row = None
                if state_exists is not None and table_exists is not None:
                    cur.execute(sql.SQL("SELECT state FROM {} WHERE asset = %s").format(
                        sql.Identifier(schema_name, STATE_TABLE)), (table_name,))
                    row = cur.fetchone()
            raw.driver_connection.rollback()
        finally:
            raw.close()
        return dict(row[0]) if row and row[0] else None

    def table_info(self, source_id: str, table: str) -> dict[str, Any]:
        """Row count, columns and content fingerprint of a staged table as it is now."""
        schema_name, table_name = self._names(source_id, table)
        raw = self._engine().raw_connection()
        try:
            with raw.driver_connection.cursor() as cur:
                ident = sql.Identifier(schema_name, table_name)
                cols = _columns_of(cur, ident)
                fp, rows = _table_fingerprint(cur, ident, [c for c, _ in cols], [t for _, t in cols])
            raw.driver_connection.rollback()
        finally:
            raw.close()
        return {"schema": schema_name, "table": table_name, "row_count": rows, "content_fingerprint": fp,
                "columns": [{"name": n, "type": t} for n, t in cols]}

    def save_state(self, source_id: str, table: str, state: StateFn) -> dict[str, Any] | None:
        """Update only the load state (an empty window still advances its watermark), under the table lock."""
        schema_name, table_name = self._names(source_id, table)
        raw = self._engine().raw_connection()
        try:
            conn = raw.driver_connection
            with conn.cursor() as cur:
                _lock(cur, schema_name, table_name)
                cur.execute("SELECT to_regclass(%s)", (f'"{schema_name}"."{table_name}"',))
                if cur.fetchone()[0] is None:
                    raise InvalidInput(f"{schema_name}.{table_name} is not staged; load it before saving its state")
                out = _write_state(cur, schema_name, table_name, state)
            conn.commit()
        except Exception:
            raw.driver_connection.rollback()
            raise
        finally:
            raw.close()
        return out

    def reconcile(self, source_id: str, table: str, key_batches: Iterable[pa.RecordBatch], *, keys: list[str],
                  policy: str, workspace_id: str, max_delete_pct: float = 50.0,
                  state: StateFn | None = None) -> dict[str, Any]:
        """Full reconcile of deletes (P6-02, ADR-0016): ``key_batches`` is every key the source holds now.
        ``reconcile`` deletes staged rows whose key is gone, ``soft`` stamps them in ``aos_deleted_at``,
        ``ignore`` only counts them. A reconcile that would remove more than ``max_delete_pct`` of the table
        is refused (an empty or partial key read is an outage, not a mass delete); nothing changes then."""
        from analystos.contracts.recipe import SOFT_DELETE_COLUMN

        if policy not in ("reconcile", "soft", "ignore"):
            raise InvalidInput("deletes must be reconcile, soft or ignore")
        schema_name, table_name = self._names(source_id, table)
        final_ident = sql.Identifier(schema_name, table_name)
        keys_ident = sql.Identifier("aos_reconcile_keys")
        raw = self._engine().raw_connection()
        try:
            conn = raw.driver_connection
            with conn.cursor() as cur:
                _lock(cur, schema_name, table_name)
                cur.execute("SELECT to_regclass(%s)", (f'"{schema_name}"."{table_name}"',))
                if cur.fetchone()[0] is None:
                    raise InvalidInput(f"{schema_name}.{table_name} is not staged; nothing to reconcile")
                cols = dict(_columns_of(cur, final_ident))
                missing = [k for k in keys if k not in cols]
                if missing:
                    raise InvalidInput(f"key column {', '.join(missing)} is not a column of {table_name}")
                if policy == "soft" and SOFT_DELETE_COLUMN not in cols:
                    raise InvalidInput(f"{table_name} has no {SOFT_DELETE_COLUMN} column; stage it with deletes: soft")
                cur.execute(sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(keys_ident, sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(k), sql.SQL(cols[k])) for k in keys)))
                received = 0
                with cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(
                        keys_ident, sql.SQL(", ").join(sql.Identifier(k) for k in keys))) as copy:
                    for batch in key_batches:
                        names = batch.schema.names
                        columns = [batch.column(names.index(k)).to_pylist() for k in keys]
                        for values in zip(*columns, strict=True):
                            copy.write_row([None if v is None else str(v) if cols[k] == "text" else v
                                            for k, v in zip(keys, values, strict=True)])
                            received += 1
                cur.execute(sql.SQL("CREATE INDEX ON {} ({})").format(keys_ident, sql.SQL(", ").join(
                    sql.Identifier(k) for k in keys)))
                gone = sql.SQL("NOT EXISTS (SELECT 1 FROM {} AS k WHERE {})").format(keys_ident, sql.SQL(" AND ").join(
                    sql.SQL("k.{k} = f.{k}").format(k=sql.Identifier(k)) for k in keys))
                live = sql.SQL(" AND f.{} IS NULL").format(sql.Identifier(SOFT_DELETE_COLUMN)) if policy == "soft" \
                    else sql.SQL("")
                cur.execute(sql.SQL("SELECT count(*) FROM {} AS f").format(final_ident))
                total = int(cur.fetchone()[0])
                cur.execute(sql.SQL("SELECT count(*) FROM {} AS f WHERE {}{}").format(final_ident, gone, live))
                missing_rows = int(cur.fetchone()[0])
                if total and missing_rows and policy != "ignore" and 100.0 * missing_rows / total > max_delete_pct:
                    raise InvalidInput(f"the reconcile would remove {missing_rows} of {total} rows of {table_name} "
                                       f"(more than {max_delete_pct:g}%); the key read looks partial, nothing was "
                                       "changed. Check the source, or raise max_delete_pct for an intended purge.",
                                       details={"missing_rows": missing_rows, "table_rows": total})
                changed = 0
                if policy == "reconcile" and missing_rows:
                    cur.execute(sql.SQL("DELETE FROM {} AS f WHERE {}").format(final_ident, gone))
                    changed = max(cur.rowcount or 0, 0)
                elif policy == "soft":
                    if missing_rows:
                        cur.execute(sql.SQL("UPDATE {} AS f SET {} = now() WHERE {}{}").format(
                            final_ident, sql.Identifier(SOFT_DELETE_COLUMN), gone, live))
                        changed = max(cur.rowcount or 0, 0)
                    # a key the source holds again is live again
                    cur.execute(sql.SQL("UPDATE {} AS f SET {} = NULL WHERE f.{} IS NOT NULL AND NOT ({})").format(
                        final_ident, sql.Identifier(SOFT_DELETE_COLUMN), sql.Identifier(SOFT_DELETE_COLUMN), gone))
                fp, rows = _table_fingerprint(cur, final_ident, list(cols), list(cols.values()))
                out: dict[str, Any] = {"policy": policy, "keys_received": received, "missing_rows": missing_rows,
                                       "deleted_rows": changed if policy == "reconcile" else 0,
                                       "soft_deleted_rows": changed if policy == "soft" else 0,
                                       "row_count": rows, "content_fingerprint": fp, "schema": schema_name,
                                       "table": table_name, "columns": [{"name": n, "type": t} for n, t in cols.items()]}
                if state is not None:
                    out["state"] = _write_state(cur, schema_name, table_name, state)
                ws_role = role_for(self.settings, workspace_id)
                ensure_workspace_role(cur, ws_role, self.reader_role)
                grant_schema(cur, schema_name, ws_role, self.reader_role)
                _hide_state(cur, schema_name, ws_role)
            conn.commit()
        except Exception:
            raw.driver_connection.rollback()
            raise
        finally:
            raw.close()
        return out

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


def _lock(cur: Any, schema_name: str, table_name: str) -> None:
    """Serialize writers of one staged table for the rest of the transaction (overlapping schedules, a retry
    racing the original): the second waits, then applies its own change to the first one's result."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"aos.load:{schema_name}.{table_name}",))


def _write_state(cur: Any, schema_name: str, table_name: str, state: StateFn) -> dict[str, Any] | None:
    ident = sql.Identifier(schema_name, STATE_TABLE)
    cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} (asset text PRIMARY KEY, state jsonb NOT NULL, "
                        "updated_at timestamptz NOT NULL DEFAULT now())").format(ident))
    cur.execute(sql.SQL("SELECT state FROM {} WHERE asset = %s FOR UPDATE").format(ident), (table_name,))
    row = cur.fetchone()
    old = dict(row[0]) if row and row[0] else None
    new = state(old)
    if new is None:
        return old
    cur.execute(sql.SQL("INSERT INTO {} (asset, state, updated_at) VALUES (%s, %s::jsonb, now()) ON CONFLICT (asset) "
                        "DO UPDATE SET state = EXCLUDED.state, updated_at = now()").format(ident),
                (table_name, json.dumps(new, default=_json_default)))
    return new


def _hide_state(cur: Any, schema_name: str, role: str) -> None:
    cur.execute("SELECT to_regclass(%s)", (f'"{schema_name}"."{STATE_TABLE}"',))
    if cur.fetchone()[0] is not None:
        cur.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(schema_name, STATE_TABLE),
                                                               sql.Identifier(role)))


def _columns_of(cur: Any, ident: sql.Composable) -> list[tuple[str, str]]:
    cur.execute(sql.SQL("SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = {}::regclass "
                        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum").format(sql.Literal(ident.as_string(cur))))
    return [(r[0], r[1]) for r in cur.fetchall()]


# (type in the new file, type of the staged column) pairs that INSERT ... SELECT converts without losing a
# value: a file whose amounts happen to be whole numbers infers bigint for a double precision column.
_WIDENS = {
    ("smallint", "integer"), ("smallint", "bigint"), ("integer", "bigint"),
    *((i, t) for i in ("smallint", "integer", "bigint") for t in ("numeric", "double precision")),
    ("real", "double precision"),
}


def _same_or_widens(new_type: str, staged_type: str) -> bool:
    return new_type == staged_type or (new_type, staged_type) in _WIDENS


def _check_same_columns(cur: Any, final_ident: sql.Composable, load_ident: sql.Composable, what: str) -> None:
    """Append and merge write into the existing table: same columns, the same types (or a lossless
    widening into the staged type), or nothing is written."""
    old, new = dict(_columns_of(cur, final_ident)), dict(_columns_of(cur, load_ident))
    problems = [f"column {c} ({t}) of the staged table is missing from {what}" for c, t in old.items() if c not in new]
    problems += [f"column {c} ({t}) of {what} is not in the staged table" for c, t in new.items() if c not in old]
    problems += [f"column {c} is {old[c]} in the staged table but {t} in {what}"
                 for c, t in new.items() if c in old and not _same_or_widens(t, old[c])]
    if problems:
        raise InvalidInput("; ".join(problems) + " (load with mode replace to change the table's shape)",
                           details={"columns": sorted({p.split()[1] for p in problems})})


def _duplicate_keys(cur: Any, ident: sql.Composable, keys: list[str]) -> int:
    cols = sql.SQL(", ").join(sql.Identifier(k) for k in keys)
    cur.execute(sql.SQL("SELECT count(*) FROM (SELECT {} FROM {} GROUP BY {} HAVING count(*) > 1) d").format(cols, ident, cols))
    return int(cur.fetchone()[0])


def _table_fingerprint(cur: Any, ident: sql.Composable, columns: list[str], types: list[str]) -> tuple[str, int]:
    """Order-independent digest of a table's rows computed in the database (a multiset hash of each
    row's text form), so a table reached by replace, append or merge hashes by content alone."""
    row = sql.Identifier("aos row")  # a space: never a sanitized column name, so it is the whole row
    cur.execute(sql.SQL("SELECT count(*), coalesce(sum(hashtextextended({}::text, 0)::numeric), 0) FROM {} AS {}")
                .format(row, ident, row))
    n, acc = cur.fetchone()
    head = json.dumps([columns, types])
    digest = hashlib.sha256(f"table|{head}|{int(n)}|{int(acc) % (1 << 64):016x}".encode()).hexdigest()
    return digest, int(n)


def _chain(first: list[pa.RecordBatch], rest: Iterable[pa.RecordBatch]):  # noqa: ANN202
    yield from first
    yield from rest
