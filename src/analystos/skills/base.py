"""What a deterministic skill receives. Skills never open connections: every byte of source data
they see arrives through the governed gateway via `RunSQL`."""
from __future__ import annotations

from typing import Protocol

from analystos.gateway.types import QueryResult


class RunSQL(Protocol):
    dialect: str  # sqlglot dialect of the source being analysed: postgres | tsql | duckdb

    def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None) -> QueryResult: ...
