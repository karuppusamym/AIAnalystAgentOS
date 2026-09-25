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
}


def normalize_sql_type(type_name: str | None) -> str:
    if not type_name:
        return "text"
    key = type_name.strip().lower()
    if key in _TYPE_MAP:
        return _TYPE_MAP[key]
    base = key.split("(", 1)[0].strip()
    return _TYPE_MAP.get(base, "text")


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
