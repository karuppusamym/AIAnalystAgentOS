from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValidatedSQL(BaseModel):
    original_sql: str
    executable_sql: str  # normalized, row-limited, in the target dialect
    dialect: str
    source_id: str
    referenced_assets: list[str]  # "schema.table"
    referenced_columns: list[str] = Field(default_factory=list)  # "schema.table.column" where resolvable
    fingerprint: str  # hash of normalized SQL (no limit), used for cache key with scope hash


class QueryResult(BaseModel):
    query_id: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    cache_hit: bool = False
    fingerprint: str
    result_hash: str
    duration_ms: int = 0
    referenced_assets: list[str] = Field(default_factory=list)
    sql: str = ""

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]
