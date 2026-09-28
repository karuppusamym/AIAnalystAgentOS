"""Embeddings without the `vector` extension (db/vectors.py): the array backend's SQL forms and its
in-process cosine ranking, which must order as an exact `ORDER BY embedding <=> q` does."""
from __future__ import annotations

import math

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from analystos.core.config import get_settings
from analystos.db import vectors


@pytest.fixture(params=["pgvector", "array"])
def backend(request, monkeypatch):
    monkeypatch.setattr(get_settings(), "vector_backend", request.param)
    return request.param


def test_sql_forms_follow_the_backend(backend):
    pg = backend == "pgvector"
    assert vectors.uses_pgvector() is pg
    assert vectors.column_sql(384) == ("vector(384)" if pg else "real[]")
    assert vectors.cast(":e") == ("CAST(:e AS vector)" if pg else "CAST(:e AS real[])")
    assert vectors.literal([0.5, -1, 1e-9]) == ("[0.5,-1,1e-09]" if pg else "{0.5,-1,1e-09}")
    assert vectors.literal(None) is None


def test_the_schema_has_no_extension_type_or_hnsw_index_on_the_array_backend(backend):
    from analystos.db.models import ContextEntry, KnowledgeSection

    dialect = postgresql.dialect()
    ddl = str(CreateTable(KnowledgeSection.__table__).compile(dialect=dialect))
    assert ("embedding VECTOR(256)" in ddl) is (backend == "pgvector")
    assert ("embedding REAL[]" in ddl) is (backend == "array")
    assert ("REAL[]" in str(CreateTable(ContextEntry.__table__).compile(dialect=dialect))) is (backend == "array")
    hnsw = next(i for i in KnowledgeSection.__table__.indexes if i.name == "ix_knowledge_section_embedding_hnsw")
    assert hnsw._ddl_if.callable_(None, None, None) is (backend == "pgvector")
    assert "hnsw" in str(CreateIndex(hnsw).compile(dialect=dialect))
    # SQLite (the unit-test schema) stores the vector as JSON either way
    assert "embedding JSON" in str(CreateTable(ContextEntry.__table__).compile(dialect=sqlite.dialect()))


def test_embedding_type_binds_and_reads_plain_float_lists():
    t = vectors.Embedding(3)
    assert t.process_bind_param((1, 2.5, 0), None) == [1.0, 2.5, 0.0]
    assert t.process_result_value([1, 2, 3], None) == [1.0, 2.0, 3.0]
    assert t.process_bind_param(None, None) is None and t.process_result_value(None, None) is None


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def test_rank_is_exact_cosine_best_first_with_ties_broken_by_id():
    q = _unit([1.0, 0.0, 0.0])
    rows = [("c", _unit([0.0, 1.0, 0.0])), ("b", _unit([1.0, 1.0, 0.0])), ("a", _unit([1.0, 1.0, 0.0])),
            ("d", _unit([1.0, 0.0, 0.0])), ("e", _unit([-1.0, 0.0, 0.0]))]
    ranked = vectors.rank(q, rows, limit=10)
    assert [i for i, _ in ranked] == ["d", "a", "b", "c", "e"]
    assert ranked[0][1] == 0.0 and ranked[-1][1] == 2.0  # distance = 1 - cosine, as pgvector's <=>
    assert ranked[1][1] == pytest.approx(1 - math.sqrt(0.5), abs=1e-6)
    assert vectors.rank(q, rows, limit=2) == ranked[:2]


def test_rank_skips_missing_vectors_other_dimensions_and_zero_vectors_sort_last():
    q = [0.6, 0.8]
    rows = [("none", None), ("3d", [1.0, 0.0, 0.0]), ("zero", [0.0, 0.0]), ("same", [0.6, 0.8]), ("half", [0.8, 0.6])]
    assert [i for i, _ in vectors.rank(q, rows, limit=10)] == ["same", "half", "zero"]
    assert vectors.rank(q, [], limit=5) == [] and vectors.rank(q, rows, limit=0) == []


def test_rank_matches_the_hashing_embeddings_used_for_the_glossary():
    """The array backend orders real glossary vectors the way cosine similarity says it should."""
    from analystos.context.embeddings import embed

    docs = {"sla": "SLA breach: an incident resolved after its service level target",
            "mttr": "Mean time to restore service after an incident",
            "rev": "Net revenue after returns and discounts"}
    rows = [(k, embed(v)) for k, v in docs.items()]
    best = vectors.rank(embed("which incidents breached the SLA"), rows, limit=3)
    assert best[0][0] == "sla" and best[-1][0] == "rev"
