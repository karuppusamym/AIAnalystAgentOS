"""Shared mapping from catalog query rows to normalized DiscoveredAsset metadata (pushdown sources).

Kept free of any database driver so the SQL Server and Postgres mappings can be unit tested with
plain rows.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn

# Lower-cased source type name -> normalized type.
_TYPE_MAP: dict[str, str] = {
    # postgres
    "smallint": "integer", "integer": "integer", "int": "integer", "int2": "integer", "int4": "integer",
    "bigint": "bigint", "int8": "bigint", "serial": "integer", "bigserial": "bigint",
    "numeric": "numeric", "decimal": "numeric", "money": "numeric",
    "real": "double", "double precision": "double", "float4": "double", "float8": "double", "float": "double",
    "boolean": "boolean", "bool": "boolean", "bit": "boolean",
    "date": "date",
    "timestamp without time zone": "timestamp", "timestamp with time zone": "timestamp", "timestamp": "timestamp",
    "timestamptz": "timestamp",
    "json": "json", "jsonb": "json",
    "text": "text", "character varying": "text", "varchar": "text", "character": "text", "char": "text",
    "uuid": "text", "name": "text", "citext": "text", "inet": "text", "time without time zone": "text",
    "time with time zone": "text", "interval": "text", "bytea": "text", "user-defined": "text", "array": "text",
    # sql server
    "tinyint": "integer", "smallmoney": "numeric", "datetime": "timestamp", "datetime2": "timestamp",
    "smalldatetime": "timestamp", "datetimeoffset": "timestamp", "nvarchar": "text", "nchar": "text",
    "ntext": "text", "uniqueidentifier": "text", "xml": "text", "varbinary": "text", "binary": "text",
    "image": "text", "time": "text", "sql_variant": "text", "hierarchyid": "text", "geography": "text",
    "geometry": "text", "rowversion": "text",
    # mysql / mariadb
    "mediumint": "integer", "year": "integer", "double": "double", "tinytext": "text", "mediumtext": "text",
    "longtext": "text", "enum": "text", "set": "text", "tinyblob": "text", "blob": "text", "mediumblob": "text",
    "longblob": "text",
    # oracle
    "number": "numeric", "binary_float": "double", "binary_double": "double", "varchar2": "text",
    "nvarchar2": "text", "clob": "text", "nclob": "text", "raw": "text", "long": "text", "rowid": "text",
    "timestamp with local time zone": "timestamp",
    # snowflake / bigquery / databricks / trino
    "timestamp_ntz": "timestamp", "timestamp_ltz": "timestamp", "timestamp_tz": "timestamp",
    "variant": "json", "object": "json", "string": "text", "int64": "bigint", "float64": "double",
    "bignumeric": "numeric", "bytes": "text", "struct": "json", "record": "json", "map": "json", "row": "json",
    "short": "integer", "byte": "integer",
    # clickhouse (wrappers Nullable()/LowCardinality() are unwrapped first)
    "int16": "integer", "int32": "integer", "uint8": "integer", "uint16": "integer",
    "uint32": "bigint", "uint64": "numeric", "int128": "numeric", "int256": "numeric", "uint128": "numeric",
    "uint256": "numeric", "float32": "double", "fixedstring": "text", "datetime64": "timestamp", "date32": "date",
    "decimal32": "numeric", "decimal64": "numeric", "decimal128": "numeric", "ipv4": "text", "ipv6": "text",
    # duckdb / sqlite
    "hugeint": "numeric", "uhugeint": "numeric", "ubigint": "numeric", "uinteger": "bigint", "usmallint": "integer",
    "utinyint": "integer", "timestamp_s": "timestamp", "timestamp_ms": "timestamp", "timestamp_ns": "timestamp",
    "list": "json", "union": "json",
}

_WRAPPERS = ("nullable(", "lowcardinality(", "simpleaggregatefunction(")
_JSON_PREFIXES = ("array<", "array(", "struct<", "struct(", "map<", "map(", "row(", "tuple(", "nested(")
_MODIFIERS = (" unsigned", " zerofill", " signed", " not null", " collate ")


def normalize_sql_type(type_name: str | None) -> str:
    """Map a source type name (as a catalog or ``TypeEngine.compile()`` prints it, any dialect) to
    the normalized vocabulary of ``DiscoveredColumn.data_type``; unknown types become ``text``."""
    if not type_name:
        return "text"
    key = " ".join(str(type_name).strip().lower().split())
    changed = True
    while changed:  # Nullable(LowCardinality(String)) -> string
        changed = False
        for wrapper in _WRAPPERS:
            if key.startswith(wrapper) and key.endswith(")"):
                key, changed = key[len(wrapper):-1].strip(), True
    if key in _TYPE_MAP:
        return _TYPE_MAP[key]
    if key.startswith(_JSON_PREFIXES) or key.endswith("[]"):
        return "json"
    if key in ("tinyint(1)", "bit(1)"):  # MySQL's boolean convention
        return "boolean"
    for modifier in _MODIFIERS:
        if modifier in key:
            key = key.split(modifier, 1)[0].strip()
    if key.startswith("number(") and key.replace(" ", "").endswith(",0)"):
        return "bigint" if _precision(key) > 9 else "integer"
    base = key.split("(", 1)[0].strip()
    if base in _TYPE_MAP:
        return _TYPE_MAP[base]
    for prefix, normalized in (("timestamp", "timestamp"), ("datetime", "timestamp"), ("interval", "text"),
                               ("time", "text"), ("decimal", "numeric"), ("numeric", "numeric")):
        if base.startswith(prefix):
            return normalized
    return "text"


def _precision(type_key: str) -> int:
    try:
        return int(type_key.split("(", 1)[1].split(",", 1)[0].strip())
    except (IndexError, ValueError):
        return 38


def build_assets(
    tables: Iterable[Mapping[str, Any]],
    columns: Iterable[Mapping[str, Any]],
    primary_keys: Iterable[Mapping[str, Any]] = (),
    foreign_keys: Iterable[Mapping[str, Any]] = (),
    row_counts: Iterable[Mapping[str, Any]] = (),
) -> list[DiscoveredAsset]:
    """Assemble assets from catalog rows.

    tables:       {schema, table, table_type ('BASE TABLE'|'VIEW'), description?}
    columns:      {schema, table, column, data_type, is_nullable ('YES'|'NO'|bool), ordinal, description?}
    primary_keys: {schema, table, column}
    foreign_keys: {schema, table, column, ref_schema, ref_table, ref_column}
    row_counts:   {schema, table, row_count (None/negative = unknown)}
    """
    pk = {(r["schema"], r["table"], r["column"]) for r in primary_keys}
    fk = {(r["schema"], r["table"], r["column"]): f"{r['ref_schema']}.{r['ref_table']}.{r['ref_column']}" for r in foreign_keys}
    counts: dict[tuple[str, str], int | None] = {}
    for r in row_counts:
        value = r.get("row_count")
        counts[(r["schema"], r["table"])] = int(value) if value is not None and float(value) >= 0 else None
    cols_by_table: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for c in columns:
        cols_by_table.setdefault((c["schema"], c["table"]), []).append(c)
    assets: list[DiscoveredAsset] = []
    for t in tables:
        key = (t["schema"], t["table"])
        table_type = str(t.get("table_type") or "BASE TABLE").upper()
        cols = sorted(cols_by_table.get(key, []), key=lambda c: int(c.get("ordinal") or 0))
        discovered = []
        for c in cols:
            nullable = c.get("is_nullable")
            if isinstance(nullable, str):
                nullable = nullable.strip().upper() in ("YES", "Y", "TRUE", "1")
            ckey = (t["schema"], t["table"], c["column"])
            discovered.append(
                DiscoveredColumn(
                    name=c["column"],
                    data_type=normalize_sql_type(c.get("data_type")),
                    nullable=bool(nullable) if nullable is not None else True,
                    is_key=ckey in pk,
                    description=c.get("description") or None,
                    references=fk.get(ckey),
                )
            )
        assets.append(
            DiscoveredAsset(
                source_name=f"{t['schema']}.{t['table']}",
                name=t["table"],
                schema_name=t["schema"],
                kind="view" if "VIEW" in table_type else "table",
                row_count=counts.get(key),
                description=t.get("description") or None,
                columns=discovered,
            )
        )
    return assets
