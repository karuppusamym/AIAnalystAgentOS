"""Connector contract (META-005 normalized metadata).

Two execution modes (ADR-0004, minimum ETL):
  pushdown  the source is SQL-queryable with a least-privilege read-only identity; the gateway
            queries it in place (PostgreSQL, SQL Server).
  staged    the source is not SQL-queryable (ServiceNow Table API, CSV/Parquet/Excel files); the
            connector extracts a bounded snapshot into the analytics DB schema `src_<source_id>`
            using the loader identity. The gateway reads it with the reader identity.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal, Protocol

import pyarrow as pa
from pydantic import BaseModel, Field


class DiscoveredColumn(BaseModel):
    name: str
    data_type: str  # normalized: integer | bigint | numeric | double | boolean | text | date | timestamp | json
    nullable: bool = True
    is_key: bool = False
    description: str | None = None
    business_name: str | None = None
    references: str | None = None  # "table.column" when the source declares a reference (FK / ServiceNow reference field)


class DiscoveredAsset(BaseModel):
    source_name: str  # name in origin system
    name: str  # name the gateway will see (sanitized identifier)
    schema_name: str | None = None  # origin schema for pushdown sources; staged sources get src_<id>
    kind: Literal["table", "view", "file", "api_table"] = "table"
    row_count: int | None = None
    description: str | None = None
    business_name: str | None = None
    freshness_at: datetime | None = None
    columns: list[DiscoveredColumn] = Field(default_factory=list)


class ConnectionTest(BaseModel):
    ok: bool
    message: str
    latency_ms: int = 0
    details: dict[str, Any] = Field(default_factory=dict)


class Connector(Protocol):
    kind: str
    execution_mode: Literal["pushdown", "staged"]
    dialect: str  # sqlglot dialect the gateway uses for this source's SQL: postgres | tsql

    def test(self) -> ConnectionTest: ...

    def discover(self) -> list[DiscoveredAsset]: ...

    def extract(self, asset: DiscoveredAsset, *, max_rows: int) -> Iterator[pa.RecordBatch]:
        """Staged connectors only: yield bounded batches of the asset's rows."""
        ...

    def sqlalchemy_url(self) -> str:
        """Pushdown connectors only: URL for the least-privilege read-only identity (secret resolved just in time)."""
        ...
