"""Embedding storage that does not assume the `vector` extension (ADR-0007, amendment 2026-09-28).

* `pgvector` keeps `vector(n)` columns, the HNSW index and `<=>` ordering in the database.
* `array` stores the same vectors as `real[]` and ranks them by exact cosine in-process (numpy), for
  a Postgres where extensions cannot be installed. The corpus is small (the sections of the packs a
  workspace may see, and a workspace's own glossary), so an exact scan costs milliseconds.

Both store unit-length vectors and report distance as 1 - cosine similarity, ties broken by id, so
the two backends rank the same way up to float rounding. The backend is chosen at install
(`ANALYSTOS_VECTOR_BACKEND`); migrations, the ORM type and every query read it from here.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import REAL, TypeDecorator

PGVECTOR = "pgvector"
ARRAY = "array"


def backend() -> str:
    from analystos.core.config import get_settings

    return get_settings().vector_backend


def uses_pgvector() -> bool:
    return backend() == PGVECTOR


def column_sql(dim: int) -> str:
    """The column's SQL type, as `format_type` prints it."""
    return f"vector({int(dim)})" if uses_pgvector() else "real[]"


def column_type(dim: int) -> Any:
    """The column type for Alembic migrations."""
    return Vector(dim) if uses_pgvector() else postgresql.ARRAY(REAL)


def cast(param: str) -> str:
    """`CAST(:e AS vector)` or `CAST(:e AS real[])`, for raw SQL that binds `literal(...)`."""
    return f"CAST({param} AS {'vector' if uses_pgvector() else 'real[]'})"


def literal(v: Sequence[float] | None) -> str | None:
    """A vector as the text form its column type parses: `[1,2]` for pgvector, `{1,2}` for real[]."""
    if v is None:
        return None
    body = ",".join(f"{float(x):.7g}" for x in v)
    return f"[{body}]" if uses_pgvector() else "{" + body + "}"


class Embedding(TypeDecorator):
    """A float vector column: `vector(dim)` or `real[]` on Postgres (by backend), JSON elsewhere
    (the SQLite unit-test schema). Binds and reads a plain list of floats either way."""

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name != "postgresql":
            return dialect.type_descriptor(JSON())
        return dialect.type_descriptor(Vector(self.dim) if uses_pgvector() else postgresql.ARRAY(REAL))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return None if value is None else [float(x) for x in value]

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return None if value is None else [float(x) for x in value]


def rank(query: Sequence[float], rows: Sequence[tuple[str, Sequence[float] | None]], limit: int) -> list[tuple[str, float]]:
    """Exact cosine ranking in-process: `(id, distance)` best first, ties by id, as an exact
    `ORDER BY embedding <=> q` would give. Rows without a vector, or with another dimension (an
    index mid re-embed), are skipped rather than compared across vector spaces."""
    q = np.asarray(query, dtype=np.float64)
    kept = [(i, v) for i, v in rows if v is not None and len(v) == len(q)]
    if not kept or limit <= 0:
        return []
    m = np.asarray([v for _, v in kept], dtype=np.float64)
    norms = np.linalg.norm(m, axis=1) * float(np.linalg.norm(q))
    sims = np.divide(m @ q, norms, out=np.zeros(len(kept)), where=norms > 0)
    scored = sorted((round(float(1.0 - s), 6), i) for (i, _), s in zip(kept, sims, strict=True))
    return [(i, d) for d, i in scored[:limit]]


def ensure_extension(conn: Any) -> None:
    """Create the `vector` extension on a fresh control database when the backend needs it."""
    if uses_pgvector():
        from sqlalchemy import text

        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
