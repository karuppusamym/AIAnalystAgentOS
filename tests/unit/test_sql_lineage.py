"""Column lineage of SQL text (P6-07): Atlas's lineage-parser suite, ported as the spec.

Ported from AIDataAnalyst@8b48fd9:tests/test_sql_lineage_parser.py (55 cases with parametrisation) onto
`analystos.evidence.lineage.sql`, which is rebuilt on sqlglot `qualify` + `lineage()`. Renames only:
`parse_view_lineage` -> `parse_lineage`, `_redact_literals` -> `redact_literals`, `_compute_sql_hash` ->
`sql_hash`, `PROCEDURE_RESULT_TARGET` -> `RESULT_TARGET`. The cases after the port are new: the CTE
case Atlas could not trace (its parser stopped at CTE names), catalog resolution and star expansion.
"""

from __future__ import annotations

import pytest

from analystos.evidence.lineage.sql import (
    FILTER_EVIDENCE_TARGET_COLUMN,
    STAR_COLUMN_MARKER,
    UNRESOLVED_TABLE,
    Confidence,
    LineageEdge,
    TransformationType,
)
from analystos.evidence.lineage.sql import RESULT_TARGET as PROCEDURE_RESULT_TARGET
from analystos.evidence.lineage.sql import parse_lineage as parse_view_lineage
from analystos.evidence.lineage.sql import redact_literals as _redact_literals
from analystos.evidence.lineage.sql import sql_hash as _compute_sql_hash

# ---------------------------------------------------------------------------
# Literal redaction
# ---------------------------------------------------------------------------


class TestRedactLiterals:
    def test_string_literals_are_redacted(self) -> None:
        sql = "SELECT * FROM t WHERE name = 'Alice'"
        redacted = _redact_literals(sql)
        assert "Alice" not in redacted
        assert "'<REDACTED>'" in redacted

    def test_numeric_literals_are_redacted(self) -> None:
        sql = "SELECT * FROM t WHERE id = 42 AND rate = 3.14"
        redacted = _redact_literals(sql)
        assert "42" not in redacted
        assert "3.14" not in redacted
        assert "<NUM>" in redacted

    def test_empty_string_literals(self) -> None:
        sql = "SELECT * FROM t WHERE name = ''"
        redacted = _redact_literals(sql)
        assert "'<REDACTED>'" in redacted


# ---------------------------------------------------------------------------
# SQL hash stability
# ---------------------------------------------------------------------------


class TestSqlHash:
    def test_hash_is_deterministic(self) -> None:
        sql = "SELECT a FROM t"
        assert _compute_sql_hash(sql) == _compute_sql_hash(sql)

    def test_different_sql_produces_different_hash(self) -> None:
        assert _compute_sql_hash("SELECT a FROM t") != _compute_sql_hash("SELECT b FROM t")

    def test_hash_is_64_hex_chars(self) -> None:
        h = _compute_sql_hash("SELECT 1")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Unsupported dialect
# ---------------------------------------------------------------------------


class TestDialectHandling:
    def test_unsupported_dialect_returns_low_confidence(self) -> None:
        result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="unknown_db")
        assert result.confidence == Confidence.LOW.value
        assert any("unsupported dialect" in e for e in result.errors)
        assert result.edges == []

    def test_postgres_dialect_accepted(self) -> None:
        result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="postgres")
        assert result.dialect == "postgres"
        assert result.confidence != Confidence.LOW.value or not result.errors

    def test_snowflake_dialect_accepted(self) -> None:
        result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="snowflake")
        assert result.dialect == "snowflake"

    def test_bigquery_dialect_accepted(self) -> None:
        result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="bigquery")
        assert result.dialect == "bigquery"


# ---------------------------------------------------------------------------
# Basic view lineage
# ---------------------------------------------------------------------------


class TestViewLineageBasic:
    def test_simple_create_view(self) -> None:
        sql = "CREATE VIEW my_view AS SELECT col_a, col_b FROM source_table"
        result = parse_view_lineage(sql, "postgres")

        assert result.confidence in (Confidence.FULL.value, Confidence.PARTIAL.value)
        assert len(result.edges) >= 2
        assert result.sql_hash
        assert result.dialect == "postgres"

        source_cols = {e.source_column for e in result.edges}
        assert "col_a" in source_cols
        assert "col_b" in source_cols

        for edge in result.edges:
            assert edge.target_table == "my_view"

    def test_aliased_columns(self) -> None:
        sql = "CREATE VIEW v AS SELECT col_a AS alias_a, col_b AS alias_b FROM t"
        result = parse_view_lineage(sql, "postgres")

        target_cols = {e.target_column for e in result.edges}
        assert "alias_a" in target_cols
        assert "alias_b" in target_cols

    def test_join_produces_edges_from_both_tables(self) -> None:
        sql = """
        CREATE VIEW v AS
        SELECT a.id, b.name
        FROM table_a a
        JOIN table_b b ON a.id = b.id
        """
        result = parse_view_lineage(sql, "postgres")

        source_tables = {e.source_table for e in result.edges}
        # Should reference both tables (possibly via aliases resolved to full names)
        assert len(source_tables) >= 1
        assert len(result.edges) >= 2

    def test_where_clause_does_not_override_a_selected_columns_own_classification(self) -> None:
        # AT-D2: `col_a` is a plain pass-through in the SELECT list *and*
        # happens to be filtered on -- those are orthogonal facts. WHERE
        # presence must never override the column's real SELECT-list
        # classification: it stays DIRECT, not FILTERED. (This test used to
        # assert the opposite -- that col_a's edge came back FILTERED -- which
        # was the bug AT-D2 reopened LN-2 to fix, not intended behaviour.)
        sql = "CREATE VIEW v AS SELECT col_a FROM t WHERE col_a > 0"
        result = parse_view_lineage(sql, "postgres")

        col_a_edges = [e for e in result.edges if e.source_column == "col_a"]
        assert len(col_a_edges) == 1
        assert col_a_edges[0].transformation_type == TransformationType.DIRECT.value
        assert col_a_edges[0].target_column == "col_a"
        # And no redundant FILTERED edge is produced for the same column --
        # it already has a real SELECT-list edge.
        assert not any(e.transformation_type == TransformationType.FILTERED.value
                        for e in result.edges)

    def test_filter_only_column_produces_filtered_evidence_not_silence(self) -> None:
        # AT-D2: `region` is referenced only in WHERE -- never in the SELECT
        # list. Previously this produced no edge at all, making a filtered
        # column indistinguishable from one the query never touched. It must
        # now get its own FILTERED evidence edge rather than being dropped.
        sql = "CREATE VIEW v AS SELECT col_a FROM t WHERE region = 1"
        result = parse_view_lineage(sql, "postgres")

        filtered = [
            e for e in result.edges if e.transformation_type == TransformationType.FILTERED.value
        ]
        assert len(filtered) == 1
        assert filtered[0].source_column == "region"
        assert filtered[0].target_column == FILTER_EVIDENCE_TARGET_COLUMN
        assert filtered[0].target_table == "v"
        # col_a keeps its own, unrelated DIRECT edge.
        col_a_edges = [e for e in result.edges if e.source_column == "col_a"]
        assert len(col_a_edges) == 1
        assert col_a_edges[0].transformation_type == TransformationType.DIRECT.value

    def test_filter_only_evidence_is_deduplicated(self) -> None:
        sql = "CREATE VIEW v AS SELECT col_a FROM t WHERE region = 1 AND region < 10"
        result = parse_view_lineage(sql, "postgres")

        filtered = [
            e for e in result.edges if e.transformation_type == TransformationType.FILTERED.value
        ]
        assert len(filtered) == 1

    def test_union_branch_where_clause_does_not_leak_into_sibling_branch(self) -> None:
        # AT-D2: FILTERED/AGGREGATED were assigned per-*statement*, so a
        # WHERE in one UNION branch could bleed onto an unrelated column in a
        # sibling branch. A column in the branch with no WHERE must never
        # come back FILTERED.
        sql = """
        CREATE VIEW v AS
        SELECT a.id FROM table_a a WHERE a.id > 0
        UNION ALL
        SELECT b.id FROM table_b b
        """
        result = parse_view_lineage(sql, "postgres")

        table_b_edges = [e for e in result.edges if e.source_table == "table_b"]
        assert len(table_b_edges) == 1
        assert table_b_edges[0].transformation_type == TransformationType.DIRECT.value

    def test_aggregation_produces_aggregated_transformation(self) -> None:
        sql = (
            "CREATE VIEW v AS SELECT department, COUNT(id) AS cnt "
            "FROM employees GROUP BY department"
        )
        result = parse_view_lineage(sql, "postgres")

        agg = [
            e for e in result.edges if e.transformation_type == TransformationType.AGGREGATED.value
        ]
        assert len(agg) >= 1

    def test_aggregation_does_not_mark_a_sibling_non_aggregated_column(self) -> None:
        # AT-D2: has_aggregation must be evaluated per-column-expression, not
        # statement-wide -- `department` (a plain grouping key, not itself
        # wrapped in an aggregate function) must stay DIRECT even though its
        # sibling `cnt` column is a genuine COUNT(...) aggregate.
        sql = (
            "CREATE VIEW v AS SELECT department, COUNT(id) AS cnt "
            "FROM employees GROUP BY department"
        )
        result = parse_view_lineage(sql, "postgres")

        department_edges = [e for e in result.edges if e.target_column == "department"]
        assert len(department_edges) == 1
        assert department_edges[0].transformation_type == TransformationType.DIRECT.value
        cnt_edges = [e for e in result.edges if e.target_column == "cnt"]
        assert len(cnt_edges) == 1
        assert cnt_edges[0].transformation_type == TransformationType.AGGREGATED.value


# ---------------------------------------------------------------------------
# CTE handling
# ---------------------------------------------------------------------------


class TestCTEHandling:
    def test_cte_lineage(self) -> None:
        sql = """
        CREATE VIEW v AS
        WITH base AS (
            SELECT id, amount FROM orders
        )
        SELECT id, amount FROM base
        """
        result = parse_view_lineage(sql, "postgres")
        assert result.confidence in (Confidence.FULL.value, Confidence.PARTIAL.value)
        # Edges should reference the original source table 'orders'
        # or the CTE 'base' -- at minimum edges are produced
        assert len(result.edges) >= 1


# ---------------------------------------------------------------------------
# UNION handling
# ---------------------------------------------------------------------------


class TestUnionHandling:
    def test_union_produces_edges_from_both_branches(self) -> None:
        sql = """
        CREATE VIEW v AS
        SELECT a.id, a.name FROM table_a a
        UNION ALL
        SELECT b.id, b.name FROM table_b b
        """
        result = parse_view_lineage(sql, "postgres")

        # UNION branches should produce edges; source table resolution may
        # use short names or aliases depending on the sqlglot dialect
        assert len(result.edges) >= 2


# ---------------------------------------------------------------------------
# SELECT * handling (AT-D2 defect 2)
# ---------------------------------------------------------------------------


class TestStarExpansion:
    def test_bare_star_produces_table_level_evidence_not_silence(self) -> None:
        # Previously a bare `continue` dropped this entirely, making a star
        # view indistinguishable from a view with zero upstreams.
        sql = "CREATE VIEW v AS SELECT * FROM t"
        result = parse_view_lineage(sql, "postgres")

        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.transformation_type == TransformationType.TABLE_STAR.value
        assert edge.source_table == "t"
        assert edge.source_column == STAR_COLUMN_MARKER
        assert edge.target_column == STAR_COLUMN_MARKER
        assert edge.source_resolved is True
        assert edge.confidence != Confidence.FULL.value  # honest, coarse evidence

    def test_qualified_star_resolves_to_its_own_table(self) -> None:
        sql = "CREATE VIEW v AS SELECT a.* FROM t a"
        result = parse_view_lineage(sql, "postgres")

        star_edges = [
            e for e in result.edges if e.transformation_type == TransformationType.TABLE_STAR.value
        ]
        assert len(star_edges) == 1
        assert star_edges[0].source_table == "t"

    def test_star_over_a_join_produces_one_edge_per_source_table(self) -> None:
        sql = "CREATE VIEW v AS SELECT * FROM table_a a JOIN table_b b ON a.id = b.id"
        result = parse_view_lineage(sql, "postgres")

        star_edges = {
            e.source_table
            for e in result.edges
            if e.transformation_type == TransformationType.TABLE_STAR.value
        }
        assert star_edges == {"table_a", "table_b"}

    def test_mixed_star_and_explicit_column_both_produce_edges(self) -> None:
        sql = "CREATE VIEW v AS SELECT *, a.id FROM t a"
        result = parse_view_lineage(sql, "postgres")

        kinds = {e.transformation_type for e in result.edges}
        assert TransformationType.TABLE_STAR.value in kinds
        assert TransformationType.DIRECT.value in kinds


# ---------------------------------------------------------------------------
# Unresolved-table sentinel (AT-D2 defect 3)
# ---------------------------------------------------------------------------


class TestUnresolvedTableSentinel:
    def test_unresolvable_reference_sets_source_resolved_false(self) -> None:
        # An unqualified column with no way to attribute it to the sole
        # FROM-clause table (sql_lineage_parser's resolution logic is
        # unchanged by AT-D2 -- only the sentinel representation is)
        sql = "CREATE VIEW v AS SELECT col_a FROM t"
        result = parse_view_lineage(sql, "postgres")

        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.source_resolved is False
        assert edge.source_table == UNRESOLVED_TABLE

    def test_resolved_reference_sets_source_resolved_true(self) -> None:
        sql = "CREATE VIEW v AS SELECT a.col_a FROM t a"
        result = parse_view_lineage(sql, "postgres")

        assert len(result.edges) == 1
        assert result.edges[0].source_resolved is True
        assert result.edges[0].source_table == "t"

    def test_unresolved_table_is_not_a_bracketed_magic_string(self) -> None:
        # The old defect was literally the string "<UNKNOWN>". The
        # replacement must not resemble it -- and, more importantly, callers
        # must be able to tell resolved from unresolved without ever
        # string-comparing source_table at all.
        assert UNRESOLVED_TABLE == "UNRESOLVED"
        assert "<" not in UNRESOLVED_TABLE
        assert ">" not in UNRESOLVED_TABLE

    def test_a_real_table_actually_named_unresolved_is_still_distinguishable(self) -> None:
        # The whole point of a typed signal instead of a magic string: even
        # if a customer's schema really does have a table called
        # "UNRESOLVED", a resolved edge naming it is still distinguishable
        # from a genuinely unresolved edge, because callers check
        # `source_resolved`, not the string value.
        genuinely_resolved = LineageEdge(
            source_table="UNRESOLVED",
            source_column="x",
            target_table="v",
            target_column="x",
            transformation_type=TransformationType.DIRECT.value,
            confidence=Confidence.FULL.value,
            dialect="postgres",
            source_resolved=True,
        )
        genuinely_unresolved = LineageEdge(
            source_table=UNRESOLVED_TABLE,
            source_column="y",
            target_table="v",
            target_column="y",
            transformation_type=TransformationType.DIRECT.value,
            confidence=Confidence.PARTIAL.value,
            dialect="postgres",
            source_resolved=False,
        )
        assert genuinely_resolved.source_table == genuinely_unresolved.source_table
        assert genuinely_resolved.source_resolved != genuinely_unresolved.source_resolved


# ---------------------------------------------------------------------------
# Differentiated confidence (AT-D2 defect 4)
# ---------------------------------------------------------------------------


class TestDifferentiatedConfidence:
    def test_fully_qualified_view_is_full_confidence(self) -> None:
        sql = "CREATE VIEW v AS SELECT a.id, a.name FROM t a"
        result = parse_view_lineage(sql, "postgres")

        assert result.confidence == Confidence.FULL.value
        assert all(e.confidence == Confidence.FULL.value for e in result.edges)

    def test_unresolved_reference_downgrades_edge_and_overall_confidence(self) -> None:
        sql = "CREATE VIEW v AS SELECT col_a FROM t"
        result = parse_view_lineage(sql, "postgres")

        assert result.confidence == Confidence.PARTIAL.value
        assert result.edges[0].confidence == Confidence.PARTIAL.value

    def test_star_expansion_downgrades_confidence(self) -> None:
        sql = "CREATE VIEW v AS SELECT * FROM t"
        result = parse_view_lineage(sql, "postgres")

        assert result.confidence != Confidence.FULL.value

    def test_confidence_is_not_hard_coded_full_regardless_of_content(self) -> None:
        # The old defect: every edge, and therefore every parse, reported
        # Confidence.FULL unconditionally. A clean, fully-qualified parse and
        # a parse leaning on unresolved/guessed evidence must now differ.
        clean = parse_view_lineage("CREATE VIEW v AS SELECT a.id FROM t a", "postgres")
        uncertain = parse_view_lineage("CREATE VIEW v AS SELECT id FROM t", "postgres")

        assert clean.confidence == Confidence.FULL.value
        assert uncertain.confidence != Confidence.FULL.value

    def test_statements_of_equal_certainty_agree_on_confidence(self) -> None:
        # Confidence is computed from what was actually resolved, not from
        # the shape of the statement -- a CREATE VIEW and a bare SELECT that
        # extract equally certain lineage get the same, non-arbitrary answer.
        view_result = parse_view_lineage("CREATE VIEW v AS SELECT a.id FROM t a", "postgres")
        select_result = parse_view_lineage("SELECT a.id FROM t a", "postgres")

        assert view_result.confidence == select_result.confidence == Confidence.FULL.value

    def test_standalone_select_target_is_reserved_marker(self) -> None:
        result = parse_view_lineage("SELECT a.id FROM t a", "postgres")
        assert result.edges[0].target_table == PROCEDURE_RESULT_TARGET


# ---------------------------------------------------------------------------
# DML statements outside a CREATE VIEW wrapper. Procedure *bodies* are
# `aida.procedure_lineage`'s job (see `tests/test_procedure_lineage.py`);
# what this module extracts from a bare INSERT/SELECT is the flat,
# per-statement sweep, nothing procedure-aware.
# ---------------------------------------------------------------------------


class TestBareStatementLineage:
    def test_insert_into_select(self) -> None:
        # Use INSERT without explicit column list to let sqlglot parse the
        # SELECT columns as the lineage target
        sql = "INSERT INTO target_table SELECT src_a, src_b FROM source_table"
        result = parse_view_lineage(sql, "postgres")

        assert len(result.edges) >= 2
        for edge in result.edges:
            assert edge.target_table == "target_table"

        source_cols = {e.source_column for e in result.edges}
        assert "src_a" in source_cols
        assert "src_b" in source_cols

    def test_standalone_select(self) -> None:
        sql = "SELECT col_a, col_b FROM t"
        result = parse_view_lineage(sql, "postgres")

        assert len(result.edges) >= 2
        for edge in result.edges:
            assert edge.target_table == "<RESULT>"

    def test_bare_insert_shares_same_confidence_levels(self) -> None:
        sql = "INSERT INTO dest (x) SELECT x FROM src"
        result = parse_view_lineage(sql, "postgres")
        assert result.confidence in (
            Confidence.FULL.value,
            Confidence.PARTIAL.value,
            Confidence.LOW.value,
        )


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_malformed_sql_returns_low_confidence_no_crash(self) -> None:
        sql = "THIS IS NOT VALID SQL @@@@"
        result = parse_view_lineage(sql, "postgres")
        # Should not raise -- graceful degradation
        assert result.confidence in (Confidence.LOW.value, Confidence.PARTIAL.value)
        assert isinstance(result.edges, list)

    def test_empty_sql_returns_low_confidence(self) -> None:
        sql = ""
        result = parse_view_lineage(sql, "postgres")
        assert isinstance(result.edges, list)


# ---------------------------------------------------------------------------
# Multi-dialect parity
# ---------------------------------------------------------------------------


class TestMultiDialect:
    @pytest.mark.parametrize("dialect", ["postgres", "snowflake", "bigquery", "tsql", "oracle"])
    def test_simple_view_parses_in_all_dialects(self, dialect: str) -> None:
        sql = "CREATE VIEW v AS SELECT a, b FROM t"
        result = parse_view_lineage(sql, dialect)

        assert result.dialect == dialect
        assert result.sql_hash
        assert isinstance(result.edges, list)
        # Should produce at least some edges (exact count may vary by dialect)
        assert len(result.edges) >= 1 or result.errors

    @pytest.mark.parametrize("dialect", ["postgres", "snowflake", "bigquery", "tsql", "oracle"])
    def test_insert_parses_in_all_dialects(self, dialect: str) -> None:
        sql = "INSERT INTO dest (x) SELECT x FROM src"
        result = parse_view_lineage(sql, dialect)

        assert result.dialect == dialect
        assert result.sql_hash


# ---------------------------------------------------------------------------
# Edge structural invariants
# ---------------------------------------------------------------------------


class TestEdgeInvariants:
    def test_all_edges_have_required_fields(self) -> None:
        sql = "CREATE VIEW v AS SELECT a, b, c FROM t"
        result = parse_view_lineage(sql, "postgres")

        for edge in result.edges:
            assert isinstance(edge.source_table, str)
            assert isinstance(edge.source_column, str)
            assert isinstance(edge.target_table, str)
            assert isinstance(edge.target_column, str)
            assert edge.transformation_type in {t.value for t in TransformationType}
            assert edge.confidence in {c.value for c in Confidence}
            assert edge.dialect

    def test_edges_are_frozen(self) -> None:
        sql = "CREATE VIEW v AS SELECT a FROM t"
        result = parse_view_lineage(sql, "postgres")
        if result.edges:
            edge = result.edges[0]
            with pytest.raises(AttributeError):
                edge.source_table = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ParseResult invariants
# ---------------------------------------------------------------------------


class TestParseResultInvariants:
    def test_result_always_has_sql_hash(self) -> None:
        for sql in ["SELECT 1", "invalid!!!", "CREATE VIEW v AS SELECT a FROM t"]:
            result = parse_view_lineage(sql, "postgres")
            assert result.sql_hash
            assert len(result.sql_hash) == 64

    def test_result_always_has_dialect(self) -> None:
        result = parse_view_lineage("SELECT 1", "postgres")
        assert result.dialect == "postgres"

    def test_result_errors_is_list(self) -> None:
        result = parse_view_lineage("SELECT 1", "postgres")
        assert isinstance(result.errors, list)


# ---------------------------------------------------------------------------
# New in AnalystOS (P6-07): through CTEs, with catalog columns, redaction
# ---------------------------------------------------------------------------


class TestThroughCTEs:
    def test_cte_is_traced_to_the_base_column(self) -> None:
        # Atlas stopped at `base`; the edge must name the table under the CTE.
        sql = """
        CREATE VIEW v AS
        WITH base AS (SELECT o.id, o.amount FROM sales.orders o WHERE o.status = 'paid')
        SELECT b.id AS order_id, b.amount FROM base b
        """
        result = parse_view_lineage(sql, "postgres")

        by_target = {e.target_column: e for e in result.edges if e.transformation_type != "FILTERED"}
        assert by_target["order_id"].source_table == "sales.orders"
        assert by_target["order_id"].source_column == "id"
        assert by_target["amount"].source_table == "sales.orders"
        assert all(e.source_resolved for e in result.edges)
        assert result.confidence == Confidence.FULL.value
        filtered = [e for e in result.edges if e.transformation_type == TransformationType.FILTERED.value]
        assert [(e.source_table, e.source_column) for e in filtered] == [("sales.orders", "status")]

    def test_chained_ctes_keep_the_strongest_transformation(self) -> None:
        sql = """
        WITH base AS (SELECT o.customer_id, o.amount FROM orders o),
             agg AS (SELECT b.customer_id, SUM(b.amount) AS total FROM base b GROUP BY b.customer_id)
        SELECT a.customer_id, a.total * 2 AS doubled FROM agg a
        """
        result = parse_view_lineage(sql, "postgres")
        by_target = {e.target_column: e for e in result.edges}
        assert by_target["doubled"].source_table == "orders"
        assert by_target["doubled"].source_column == "amount"
        assert by_target["doubled"].transformation_type == TransformationType.AGGREGATED.value
        assert by_target["customer_id"].transformation_type == TransformationType.DIRECT.value

    def test_derived_expression_is_derived(self) -> None:
        result = parse_view_lineage("SELECT o.amount * 0.9 AS net FROM orders o", "postgres")
        assert result.edges[0].transformation_type == TransformationType.DERIVED.value


class TestCatalogColumns:
    CATALOG = {"sales.orders": ["id", "amount", "status"], "sales.customers": ["id", "region"]}

    def test_catalog_resolves_unqualified_columns(self) -> None:
        sql = "WITH base AS (SELECT id, amount FROM sales.orders) SELECT id, amount FROM base"
        without = parse_view_lineage(sql, "postgres")
        assert all(not e.source_resolved for e in without.edges)
        with_catalog = parse_view_lineage(sql, "postgres", catalog=self.CATALOG)
        assert {(e.source_table, e.source_column) for e in with_catalog.edges} == {("sales.orders", "id"),
                                                                                    ("sales.orders", "amount")}
        assert with_catalog.confidence == Confidence.FULL.value

    def test_catalog_expands_stars_to_columns(self) -> None:
        result = parse_view_lineage("SELECT * FROM sales.customers", "postgres", catalog=self.CATALOG)
        assert {(e.source_column, e.target_column) for e in result.edges} == {("id", "id"), ("region", "region")}
        assert all(e.transformation_type == TransformationType.DIRECT.value for e in result.edges)

    def test_ambiguous_column_without_catalog_is_unresolved_and_with_catalog_is_not(self) -> None:
        sql = "SELECT region FROM sales.orders o JOIN sales.customers c ON o.id = c.id"
        assert parse_view_lineage(sql, "postgres").edges[0].source_resolved is False
        edge = parse_view_lineage(sql, "postgres", catalog=self.CATALOG).edges[0]
        assert (edge.source_table, edge.source_column, edge.source_resolved) == ("sales.customers", "region", True)


class TestRedactionBeforeStorage:
    def test_escaped_and_dollar_quoted_strings_are_redacted(self) -> None:
        sql = "SELECT o.id FROM orders o WHERE o.note = 'it''s secret' AND o.body = $$pii here$$ AND o.n = 7"
        red = _redact_literals(sql)
        assert "secret" not in red and "pii here" not in red and " 7" not in red
        assert red.count("'<REDACTED>'") == 2 and "<NUM>" in red

    def test_parse_result_carries_only_redacted_sql(self) -> None:
        result = parse_view_lineage("SELECT o.id FROM orders o WHERE o.email = 'ada@example.com'", "postgres")
        assert "ada@example.com" not in result.redacted_sql
        assert result.sql_hash == _compute_sql_hash("SELECT o.id FROM orders o WHERE o.email = 'someone@else.org'")
