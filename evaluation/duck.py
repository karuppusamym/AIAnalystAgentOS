"""RunSQL over an in-memory DuckDB for the component tier of the benchmark: the same skills code
the gateway feeds, over a local table (no services). The platform tier uses the real gateway."""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from analystos.gateway.types import QueryResult


class DuckRunSQL:
    dialect = "duckdb"

    def __init__(self, con: Any):
        self.con = con

    def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None) -> QueryResult:
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        truncated = max_rows is not None and len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]
        return QueryResult(query_id=f"q_{uuid.uuid4().hex[:12]}", columns=cols, rows=[list(r) for r in rows],
                           row_count=len(rows), truncated=truncated, fingerprint=hashlib.sha256(sql.encode()).hexdigest(),
                           result_hash=hashlib.sha256(repr(rows).encode()).hexdigest(), sql=sql)
