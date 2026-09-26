"""Column-level lineage of one SQL statement (P6-07), on sqlglot `qualify` + `lineage()`.

Rewrite of Atlas `sql_lineage_parser.py` (AIDataAnalyst@8b48fd9:src/aida/sql_lineage_parser.py),
whose tests are the spec (`tests/unit/test_sql_lineage.py` ports them). Atlas resolved columns with
its own walker and stopped at CTE names, so a view built on a CTE had lineage to the CTE, not to the
table under it. Here every output column is traced by sqlglot's `lineage()` through CTEs, derived
tables and set operations to the base table it is read from.

Resolution is honest about what the SQL proves. A reference is *resolved* when the SQL qualifies it
(`o.amount`) or when a catalog passed in names the column on that table; otherwise the edge keeps
the column name but its table is the cosmetic `UNRESOLVED` label with `source_resolved=False` (Atlas
AT-D2), and confidence drops to PARTIAL. With catalog columns, `SELECT *` expands to one edge per
column; without, it becomes table-level `TABLE_STAR` evidence. A column used only in a WHERE clause
gets a `FILTERED` evidence edge. Literals are redacted before anything is stored or hashed.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

UNRESOLVED_TABLE: Final[str] = "UNRESOLVED"  # cosmetic; `LineageEdge.source_resolved` is the signal
RESULT_TARGET: Final[str] = "<RESULT>"  # target of a standalone SELECT
FILTER_EVIDENCE_TARGET_COLUMN: Final[str] = "<FILTER_PREDICATE>"
STAR_COLUMN_MARKER: Final[str] = "*"
SUPPORTED_DIALECTS: Final[frozenset[str]] = frozenset({
    "postgres", "redshift", "snowflake", "bigquery", "tsql", "oracle", "duckdb", "mysql", "databricks", "spark",
    "trino", "clickhouse", "sqlite"})
_QUALIFIED = "aos_qualified"
_STRING_TOKENS = {"STRING", "NATIONAL_STRING", "RAW_STRING", "HEREDOC_STRING", "BIT_STRING", "HEX_STRING", "BYTE_STRING",
                  "UNICODE_STRING"}


class TransformationType(StrEnum):
    DIRECT = "DIRECT"
    DERIVED = "DERIVED"
    AGGREGATED = "AGGREGATED"
    FILTERED = "FILTERED"
    TABLE_STAR = "TABLE_STAR"


class Confidence(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class LineageEdge:
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    transformation_type: str
    confidence: str
    dialect: str
    source_resolved: bool = True


@dataclass
class ParseResult:
    edges: list[LineageEdge]
    confidence: str
    dialect: str
    sql_hash: str
    redacted_sql: str
    target_table: str | None = None
    errors: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------ redaction
def redact_literals(sql: str, dialect: str = "postgres") -> str:
    """String and numeric literals replaced with `'<REDACTED>'` and `<NUM>` (token-exact: escaped
    quotes and dollar-quoted strings included), so no data value in a definition is ever stored."""
    if not sql:
        return ""
    try:
        from sqlglot.dialects.dialect import Dialect

        tokens = Dialect.get_or_raise(dialect if dialect in SUPPORTED_DIALECTS else "postgres").tokenize(sql)
    except (SqlglotError, ValueError):
        redacted = re.sub(r"'(?:[^']|'')*'", "'<REDACTED>'", sql)
        return re.sub(r"\b\d+(?:\.\d+)?\b", "<NUM>", redacted)
    out, pos = [], 0
    for tok in tokens:
        kind = tok.token_type.name
        if kind in _STRING_TOKENS:
            repl = "'<REDACTED>'"
        elif kind == "NUMBER":
            repl = "<NUM>"
        else:
            continue
        out.append(sql[pos:tok.start])
        out.append(repl)
        pos = tok.end + 1
    out.append(sql[pos:])
    return "".join(out)


def sql_hash(sql: str, dialect: str = "postgres") -> str:
    """SHA-256 of the redacted definition: equal definitions hash equal, no value leaks through it."""
    return hashlib.sha256(redact_literals(sql or "", dialect).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------ helpers
def table_name(t: exp.Table) -> str:
    return ".".join(p for p in (t.catalog, t.db, t.name) if p)


def _catalog_mapping(catalog: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, set[str]]]:
    """{"schema.table": [cols] | {col: type}} -> the nested mapping sqlglot wants (the most common
    depth wins; sqlglot needs one depth) and a flat {table name: columns} index."""
    if not catalog:
        return None, {}
    flat = {k.lower(): {c.lower() for c in (v.keys() if isinstance(v, dict) else v)} for k, v in catalog.items()}
    depth = Counter(k.count(".") for k in flat).most_common(1)[0][0]
    nested: dict[str, Any] = {}
    for key, value in catalog.items():
        parts = key.lower().split(".")
        if len(parts) - 1 != depth:
            continue
        node = nested
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = {c.lower(): (t if isinstance(value, dict) else "UNKNOWN")
                           for c, t in (value.items() if isinstance(value, dict) else ((c, None) for c in value))}
    return nested, flat


def _in_catalog(flat: dict[str, set[str]], table: str, column: str) -> bool:
    cols = flat.get(table.lower())
    if cols is None:  # the catalog may name the table with fewer parts than the SQL
        cols = next((v for k, v in flat.items() if table.lower().endswith("." + k) or k.endswith("." + table.lower())), None)
    return bool(cols) and column.lower() in cols


def _target_and_query(tree: exp.Expression) -> tuple[str, list[str] | None, exp.Query] | None:
    if isinstance(tree, exp.Create) and isinstance(tree.expression, exp.Query):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        cols = [i.name for i in tree.this.expressions] if isinstance(tree.this, exp.Schema) else None
        return (table_name(target) if isinstance(target, exp.Table) else target.sql()), cols or None, tree.expression
    if isinstance(tree, exp.Insert) and isinstance(tree.expression, exp.Query):
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        cols = [i.name for i in tree.this.expressions] if isinstance(tree.this, exp.Schema) else None
        return (table_name(target) if isinstance(target, exp.Table) else target.sql()), cols or None, tree.expression
    if isinstance(tree, exp.Query):
        return RESULT_TARGET, None, tree
    return None


def _branches(query: exp.Query) -> list[exp.Select]:
    if isinstance(query, exp.SetOperation):
        return [*_branches(query.left), *_branches(query.right)]
    if isinstance(query, exp.Subquery):
        return _branches(query.this)
    return [query] if isinstance(query, exp.Select) else []


def _base_tables(select: exp.Select, ctes: dict[str, exp.Query], alias: str | None = None, seen: frozenset = frozenset()) -> list[str]:
    """Base tables a star over `select` (or over its source `alias`) reads."""
    out: list[str] = []
    sources: list[exp.Expression] = []
    from_ = select.args.get("from") or select.args.get("from_")
    if from_ is not None:
        sources.append(from_.this)
    sources += [j.this for j in select.args.get("joins") or []]
    for src in sources:
        if alias and src.alias_or_name != alias:
            continue
        if isinstance(src, exp.Table):
            if not src.db and src.name in ctes and src.name not in seen:
                for b in _branches(ctes[src.name]):
                    out += _base_tables(b, ctes, seen=seen | {src.name})
            else:
                out.append(table_name(src))
        elif isinstance(src, exp.Subquery):
            for b in _branches(src.this):
                out += _base_tables(b, ctes, seen=seen)
    return list(dict.fromkeys(out))


def _projection(node_expr: exp.Expression, position: int) -> exp.Expression:
    """The projection a lineage node stands for (a union branch node carries the whole SELECT)."""
    if isinstance(node_expr, exp.Select):
        sel = node_expr.selects
        return sel[position] if position < len(sel) else node_expr
    return node_expr


def _classify(path: list[Any], position: int) -> str:
    kind = TransformationType.DIRECT
    for node in path:
        if isinstance(node.expression, (exp.Table, exp.SetOperation)):
            continue
        proj = _projection(node.expression, position)
        body = proj.this if isinstance(proj, exp.Alias) else proj
        if any(isinstance(n, exp.AggFunc) for n in body.walk()):
            return TransformationType.AGGREGATED.value
        if not isinstance(body, exp.Column) and not isinstance(body, exp.Select):
            kind = TransformationType.DERIVED
    return kind.value


def _was_qualified(parent: Any, alias: str, column: str, position: int) -> bool | None:
    proj = _projection(parent.expression, position)
    for c in proj.find_all(exp.Column):
        if c.name == column and (c.table == alias or not c.table):
            return bool(c.meta.get(_QUALIFIED))
    return None


# ------------------------------------------------------------------------------------ parse
def parse_lineage(sql: str, dialect: str = "postgres", *, catalog: dict[str, Any] | None = None) -> ParseResult:
    """Column lineage of a CREATE VIEW/TABLE AS, INSERT ... SELECT or standalone query. Never raises:
    an unsupported dialect or an unparseable statement is a LOW-confidence result with errors."""
    from sqlglot.lineage import lineage
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import build_scope, traverse_scope

    digest = sql_hash(sql, dialect if dialect in SUPPORTED_DIALECTS else "postgres")
    redacted = redact_literals(sql or "", dialect if dialect in SUPPORTED_DIALECTS else "postgres")
    result = ParseResult(edges=[], confidence=Confidence.LOW.value, dialect=dialect, sql_hash=digest, redacted_sql=redacted)
    if dialect not in SUPPORTED_DIALECTS:
        result.errors.append(f"unsupported dialect {dialect!r}")
        return result
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (SqlglotError, ValueError) as exc:
        result.errors.append(f"parse error: {str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__}")
        return result
    shape = _target_and_query(tree) if tree is not None else None
    if shape is None:
        result.errors.append(f"unsupported statement ({type(tree).__name__}): lineage needs a query, CREATE ... AS or "
                             "INSERT ... SELECT")
        return result
    target, target_cols, query = shape
    result.target_table = target
    for c in query.find_all(exp.Column):
        c.meta[_QUALIFIED] = bool(c.table)
    mapping, flat = _catalog_mapping(catalog)
    try:
        qualified = qualify(query.copy(), schema=mapping, dialect=dialect, validate_qualify_columns=False,
                            quote_identifiers=False, identify=False, expand_stars=bool(mapping))
    except (SqlglotError, ValueError, KeyError) as exc:
        result.errors.append(f"qualify failed: {str(exc).splitlines()[0][:200]}")
        return result
    ctes = {c.alias_or_name: c.this for c in qualified.find_all(exp.CTE)}
    branches = _branches(qualified)
    if not branches:
        result.errors.append("no SELECT to trace")
        return result
    edges: list[LineageEdge] = []
    seen_edges: set[LineageEdge] = set()

    def edge(src: str, col: str, tgt_col: str, kind: str, resolved: bool, full: bool) -> LineageEdge:
        return LineageEdge(source_table=src if resolved else UNRESOLVED_TABLE, source_column=col, target_table=target,
                           target_column=tgt_col, transformation_type=kind,
                           confidence=Confidence.FULL.value if full and resolved else Confidence.PARTIAL.value,
                           dialect=dialect, source_resolved=resolved)

    # star projections (no catalog to expand them): table-level evidence per base table, per branch
    for branch in branches:
        for proj in branch.selects:
            star = isinstance(proj, exp.Star) or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star))
            if not star:
                continue
            for base in _base_tables(branch, ctes, alias=proj.table if isinstance(proj, exp.Column) else None):
                e = edge(base, STAR_COLUMN_MARKER, STAR_COLUMN_MARKER, TransformationType.TABLE_STAR.value, True, False)
                if e not in seen_edges:
                    seen_edges.add(e)
                    edges.append(e)
    names = [p.alias_or_name for p in branches[0].selects]
    try:  # every column in one walk (a shared cache; one qualify, not one per column)
        roots: dict[str, Any] = lineage(None, qualified, schema=mapping, dialect=dialect, scope=build_scope(qualified),
                                       trim_selects=False)
    except (SqlglotError, ValueError, KeyError):
        roots = {}
    for position, name in enumerate(names):
        proj = branches[0].selects[position]
        if isinstance(proj, exp.Star) or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)):
            continue
        tgt = target_cols[position] if target_cols and position < len(target_cols) else name
        root = roots.get(name)
        if root is None:
            try:
                root = lineage(name, qualified, schema=mapping, dialect=dialect, trim_selects=False)
            except (SqlglotError, ValueError, KeyError) as exc:
                result.errors.append(f"column {name}: {str(exc).splitlines()[0][:160]}")
                continue
        stack: list[tuple[Any, list[Any]]] = [(root, [])]
        while stack:
            node, path = stack.pop()
            if node.downstream:
                stack.extend((d, [*path, node]) for d in node.downstream)
                continue
            if not path:
                continue  # a literal projection has no source
            parent = path[-1]
            alias, _, col = node.name.rpartition(".")
            if isinstance(node.source, exp.Table):
                src = table_name(node.source)
                qualified_ref = _was_qualified(parent, alias, col, position)
                in_cat = _in_catalog(flat, src, col)
                resolved = bool(qualified_ref) or in_cat
                full = resolved
            else:
                src, resolved, full = UNRESOLVED_TABLE, False, False
            e = edge(src, col or node.name, tgt, _classify(path, position), resolved, full)
            if e not in seen_edges:
                seen_edges.add(e)
                edges.append(e)
    # columns that only decide which rows exist (WHERE), per scope
    sourced = {(e.source_table, e.source_column) for e in edges} | {(None, e.source_column) for e in edges if not e.source_resolved}
    try:
        scopes = list(traverse_scope(qualified))
    except (SqlglotError, ValueError):
        scopes = []
    for scope in scopes:
        where = scope.expression.args.get("where") if isinstance(scope.expression, exp.Select) else None
        if where is None:
            continue
        for c in where.find_all(exp.Column):
            if c.find_ancestor(exp.Select) is not scope.expression:
                continue
            source = scope.sources.get(c.table)
            if not isinstance(source, exp.Table):
                continue
            src = table_name(source)
            resolved = bool(c.meta.get(_QUALIFIED)) or _in_catalog(flat, src, c.name)
            key = (src, c.name) if resolved else (None, c.name)
            if key in sourced:
                continue
            sourced.add(key)
            edges.append(edge(src, c.name, FILTER_EVIDENCE_TARGET_COLUMN, TransformationType.FILTERED.value, resolved,
                              resolved))
    result.edges = edges
    if result.errors and not edges:
        result.confidence = Confidence.LOW.value
    elif all(e.confidence == Confidence.FULL.value for e in edges) and not result.errors:
        result.confidence = Confidence.FULL.value
    else:
        result.confidence = Confidence.PARTIAL.value
    return result
