"""Compile an `AnalysisSpec` into governed, dialect-specific, pushdown SQL.

Everything here builds sqlglot expression trees, never string-concatenated SQL:

* identifiers are always quoted (`"col"` / `[col]`), so a column called `order` or containing a
  quote cannot change the statement's shape;
* literal values (filter values, `equals` values, bucket labels) go through sqlglot literal
  nodes, so quotes are escaped by the generator.

Supported dialects: ``postgres``, ``tsql`` (SQL Server) and ``duckdb``. For each dialect every
`Derivation` type is implemented with functions that exist in that engine:

=============== ================================== ===================================================
derivation      postgres / duckdb                  tsql
=============== ================================== ===================================================
duration_hours  (EXTRACT(EPOCH FROM end)           DATEDIFF_BIG(SECOND, start, end) / 3600.0
                 - EXTRACT(EPOCH FROM start))/3600
after_hours     EXTRACT(HOUR ..) / EXTRACT(DOW ..) DATEPART(HOUR, ..) and a DATEFIRST-independent
                                                   day-of-week: DATEDIFF(DAY, '1900-01-07', ts) % 7
date_trunc      DATE_TRUNC('grain', ts)            DATEADD/DATEDIFF from 1900-01-01 (works on every
                                                   SQL Server version, unlike DATETRUNC which is 2022+)
hour_of_day     EXTRACT(HOUR FROM ts)              DATEPART(HOUR, ts)
day_of_week     EXTRACT(DOW FROM ts), 0 = Sunday   DATEDIFF(DAY, '1900-01-07', ts) % 7, 0 = Sunday
bucket          CASE on edges -> string-sortable   same
                label ("0","1","2","3+"; zero-
                padded like "05-10" when edges
                have more digits) + numeric order
equals/is_true  CASE -> 1 / 0 / NULL                same (no boolean type needed)
=============== ================================== ===================================================

Timestamp inputs are CAST to TIMESTAMP (DATETIME2 on tsql) so text-typed timestamps (common in
ServiceNow extracts) work. Boolean derivations evaluate to integer 1/0 (NULL when the input is NULL),
which makes `SUM()` count positives and `AVG()` compute a rate on every engine.

Each analysis method (`analystos.methods`) assembles its queries from these primitives: *aggregated*
SQL where its statistic only needs aggregates (rates, series, volumes, numeric summaries) and a
*bounded, deterministic row sample* (`sample_query`) where it needs row-level values (distributions,
pairs, driver rows). The sample is
ordered by a hash of the selected values plus a per-duplicate row number (so identical tuples are
still sampled row-by-row) and limited to ``sample_rows``: re-running on unchanged data returns the
same sample, and nothing ever pulls a full table. Rows whose outcome/segment/driver derive to NULL
are excluded from the analysis *and counted* (``n_rows``/``n`` columns in aggregates;
``_rows_total``/``_rows_excluded`` window counts in samples).
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlglot
from sqlglot import exp

from analystos.contracts.analysis import AnalysisSpec, Derivation, Filter
from analystos.core.errors import InvalidInput

DIALECTS = ("postgres", "tsql", "duckdb")
BOOLEAN_DERIVATIONS = frozenset({"after_hours", "equals", "is_true"})
TIME_DERIVATIONS = frozenset({"duration_hours", "after_hours", "date_trunc", "hour_of_day", "day_of_week"})
TRUE_TEXT = ("true", "t", "1", "yes", "y")

MAX_GROUPS = 1000  # cap on GROUP BY result rows for segment aggregates
MAX_PERIODS = 5000
MAX_PARETO_SEGMENTS = 5000


@dataclass
class CompiledQuery:
    sql: str
    columns: dict[str, str]  # role -> output column alias
    notes: list[str] = field(default_factory=list)
    purpose: str = "primary"
    dialect: str = "duckdb"
    max_rows: int | None = None
    kind: str = "aggregate"  # aggregate | sample


# --------------------------------------------------------------------------------------------
# small expression helpers
# --------------------------------------------------------------------------------------------
def _check_dialect(dialect: str) -> str:
    d = (dialect or "").lower()
    if d in ("mssql", "sqlserver"):
        d = "tsql"
    if d in ("postgresql", "pg"):
        d = "postgres"
    if d not in DIALECTS:
        raise InvalidInput(f"unsupported dialect {dialect!r}; expected one of {DIALECTS}")
    return d


def col(name: str) -> exp.Column:
    if not name or not isinstance(name, str):
        raise InvalidInput("column name must be a non-empty string")
    return exp.Column(this=exp.to_identifier(name, quoted=True))


def ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def table(asset: str, alias: str | None = None) -> exp.Table:
    """'schema.table' (or 'db.schema.table' / 'table') -> quoted table reference."""
    if not asset or not isinstance(asset, str):
        raise InvalidInput("asset must be 'schema.table'")
    parts = asset.split(".")
    if len(parts) > 3 or any(not p for p in parts):
        raise InvalidInput(f"invalid asset name {asset!r}")
    kwargs: dict[str, Any] = {"this": ident(parts[-1])}
    if len(parts) >= 2:
        kwargs["db"] = ident(parts[-2])
    if len(parts) == 3:
        kwargs["catalog"] = ident(parts[0])
    t = exp.Table(**kwargs)
    if alias:
        t = t.as_(ident(alias))  # type: ignore[assignment]
    return t


def lit(value: Any, dialect: str = "duckdb") -> exp.Expression:
    """Python value -> escaped SQL literal."""
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        if dialect == "tsql":
            return exp.Literal.number(1 if value else 0)
        return exp.Boolean(this=value)
    if isinstance(value, int | float | Decimal):
        if isinstance(value, float) and value != value:  # NaN
            raise InvalidInput("NaN is not a valid SQL literal")
        return exp.Literal.number(value)
    if isinstance(value, _dt.datetime):
        return exp.Literal.string(value.isoformat(sep=" "))
    if isinstance(value, _dt.date):
        return exp.Literal.string(value.isoformat())
    if isinstance(value, str):
        return exp.Literal.string(value)
    raise InvalidInput(f"unsupported literal type {type(value).__name__}")


def fn(name: str, *args: exp.Expression) -> exp.Anonymous:
    return exp.Anonymous(this=name, expressions=list(args))


def num(v: float | int) -> exp.Literal:
    return exp.Literal.number(v)


def _dtype(kind: str, dialect: str) -> exp.DataType:
    if kind == "timestamp":
        return exp.DataType.build("DATETIME2" if dialect == "tsql" else "TIMESTAMP", dialect=dialect)
    if kind == "double":
        return exp.DataType.build("FLOAT" if dialect == "tsql" else "DOUBLE PRECISION" if dialect == "postgres" else "DOUBLE", dialect=dialect)
    if kind == "text":
        return exp.DataType.build("NVARCHAR(4000)" if dialect == "tsql" else "VARCHAR", dialect=dialect)
    if kind == "date":
        return exp.DataType.build("DATE", dialect=dialect)
    raise ValueError(kind)


def cast(e: exp.Expression, kind: str, dialect: str) -> exp.Cast:
    return exp.Cast(this=e, to=_dtype(kind, dialect))


def paren(e: exp.Expression) -> exp.Paren:
    return exp.Paren(this=e)


def case(whens: list[tuple[exp.Expression, exp.Expression]], default: exp.Expression | None) -> exp.Case:
    c = exp.Case()
    for cond, val in whens:
        c = c.when(cond, val)
    if default is not None:
        c = c.else_(default)
    return c


def is_null(e: exp.Expression) -> exp.Is:
    return exp.Is(this=e, expression=exp.Null())


def not_null(e: exp.Expression) -> exp.Not:
    return exp.Not(this=is_null(e))


def and_all(conds: list[exp.Expression]) -> exp.Expression | None:
    conds = [c for c in conds if c is not None]
    if not conds:
        return None
    out = conds[0]
    for c in conds[1:]:
        out = exp.And(this=out, expression=c)
    return out


def or_all(conds: list[exp.Expression]) -> exp.Expression:
    out = conds[0]
    for c in conds[1:]:
        out = exp.Or(this=out, expression=c)
    return out


def count_star() -> exp.Count:
    return exp.Count(this=exp.Star())


def to_sql(e: exp.Expression, dialect: str) -> str:
    return e.sql(dialect=_check_dialect(dialect))


# --------------------------------------------------------------------------------------------
# time primitives per dialect
# --------------------------------------------------------------------------------------------
_TSQL_EPOCH = "1900-01-01"  # a Monday (ISO literal: language-independent for DATETIME2)
_TSQL_SUNDAY = "1900-01-07"  # a Sunday


def ts_expr(column: str, dialect: str) -> exp.Expression:
    return cast(col(column), "timestamp", dialect)


def hour_expr(ts: exp.Expression, dialect: str) -> exp.Expression:
    if dialect == "tsql":
        return fn("DATEPART", exp.var("HOUR"), ts)
    return exp.Extract(this=exp.var("HOUR"), expression=ts)


def dow_expr(ts: exp.Expression, dialect: str) -> exp.Expression:
    """0 = Sunday ... 6 = Saturday on every dialect."""
    if dialect == "tsql":
        # DATEFIRST-independent: days since a known Sunday, mod 7.
        return paren(exp.Mod(this=fn("DATEDIFF", exp.var("DAY"), cast(lit(_TSQL_SUNDAY), "timestamp", dialect), ts),
                             expression=num(7)))
    return exp.Extract(this=exp.var("DOW"), expression=ts)


def trunc_expr(ts: exp.Expression, grain: str, dialect: str) -> exp.Expression:
    grain = (grain or "month").lower()
    if grain not in ("day", "week", "month", "quarter"):
        raise InvalidInput(f"unsupported time grain {grain!r}")
    if dialect != "tsql":
        return fn("DATE_TRUNC", lit(grain), ts)
    base = cast(lit(_TSQL_EPOCH), "timestamp", dialect)
    if grain == "day":
        return cast(cast(ts, "date", dialect), "timestamp", dialect)
    if grain == "week":  # Monday-start weeks, like postgres/duckdb date_trunc('week')
        days = paren(exp.Mod(this=fn("DATEDIFF", exp.var("DAY"), base, ts), expression=num(7)))
        return fn("DATEADD", exp.var("DAY"), exp.Neg(this=days), cast(cast(ts, "date", dialect), "timestamp", dialect))
    unit = exp.var(grain.upper())
    return fn("DATEADD", unit, fn("DATEDIFF", unit, base, ts), base.copy())


def epoch_hours_diff(start: exp.Expression, end: exp.Expression, dialect: str) -> exp.Expression:
    if dialect == "tsql":
        return exp.Div(this=fn("DATEDIFF_BIG", exp.var("SECOND"), start, end), expression=num(3600.0))
    diff = exp.Sub(this=exp.Extract(this=exp.var("EPOCH"), expression=end),
                   expression=exp.Extract(this=exp.var("EPOCH"), expression=start))
    return exp.Div(this=paren(diff), expression=num(3600.0))


# --------------------------------------------------------------------------------------------
# derivations
# --------------------------------------------------------------------------------------------
def _edge_width(edges: list[float]) -> int:
    return max(len(str(int(abs(e)))) for e in edges)


def _fmt_edge(v: float, width: int = 1) -> str:
    """Zero-pad the integer part to `width` so labels sort as strings (non-negative edges)."""
    if v < 0:
        return str(int(v)) if float(v).is_integer() else f"{v:g}"
    if float(v).is_integer():
        return str(int(v)).zfill(width)
    ip, frac = f"{v:.10g}".split(".") if "." in f"{v:.10g}" else (f"{v:.10g}", "")
    return ip.zfill(width) + ("." + frac if frac else "")


def bucket_labels(edges: list[float]) -> list[str]:
    """Labels for intervals [lo, hi) plus a final open bucket.

    Edges [0,1,2,3] -> ["0","1","2","3+"]; [0,5,10,20] -> ["00-05","05-10","10-20","20+"]. The integer
    part of every edge is zero-padded to the widest edge so that, for non-negative edges, the labels
    sort correctly *as strings* (a chart or BI tool ordering the dimension alphabetically shows the
    buckets in numeric order). Values below the first edge get "(<e0)", which also sorts first
    ('(' < '0'). The compiler additionally emits a numeric `segment_order` column.
    """
    w = _edge_width(edges)
    labels = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=False):
        if float(lo).is_integer() and float(hi).is_integer() and hi - lo == 1:
            labels.append(_fmt_edge(lo, w))
        else:
            labels.append(f"{_fmt_edge(lo, w)}-{_fmt_edge(hi, w)}")
    labels.append(f"{_fmt_edge(edges[-1], w)}+")
    return labels


def below_label(edges: list[float]) -> str:
    return f"(<{_fmt_edge(edges[0], _edge_width(edges))})"


def _validated_edges(d: Derivation) -> list[float]:
    if not d.edges:
        raise InvalidInput("bucket derivation requires `edges`")
    edges = [float(e) for e in d.edges]
    if any(b <= a for a, b in zip(edges[:-1], edges[1:], strict=False)):
        raise InvalidInput("bucket edges must be strictly increasing")
    return edges


def bucket_exprs(d: Derivation, dialect: str) -> tuple[exp.Expression, exp.Expression]:
    """(label CASE, sortable order CASE) for a bucket derivation."""
    edges = _validated_edges(d)
    labels = bucket_labels(edges)
    x = col(d.column)
    below = below_label(edges)
    lab_whens: list[tuple[exp.Expression, exp.Expression]] = [(is_null(x), exp.Null()),
                                                               (exp.LT(this=x.copy(), expression=num(edges[0])), lit(below))]
    ord_whens: list[tuple[exp.Expression, exp.Expression]] = [(is_null(x.copy()), exp.Null()),
                                                               (exp.LT(this=x.copy(), expression=num(edges[0])), num(-1))]
    for i, hi in enumerate(edges[1:]):
        lab_whens.append((exp.LT(this=x.copy(), expression=num(hi)), lit(labels[i])))
        ord_whens.append((exp.LT(this=x.copy(), expression=num(hi)), num(i)))
    return case(lab_whens, lit(labels[-1])), case(ord_whens, num(len(edges) - 1))


def is_true_expr(e: exp.Expression, dialect: str) -> exp.Expression:
    """Boolean-ish value -> 1/0/NULL. Handles native booleans, bits and 'true'/'false'/'1'/'yes' text."""
    txt = exp.Lower(this=exp.Trim(this=cast(e.copy(), "text", dialect)))
    cond = exp.In(this=txt, expressions=[lit(v) for v in TRUE_TEXT])
    return case([(is_null(e.copy()), exp.Null()), (cond, num(1))], num(0))


def derive(d: Derivation, dialect: str) -> exp.Expression:
    """The SQL expression for one derivation (value only; see `bucket_exprs` for bucket order)."""
    dialect = _check_dialect(dialect)
    t = d.type
    if t == "column":
        return col(d.column)
    if t == "duration_hours":
        if not d.end_column:
            raise InvalidInput("duration_hours requires `end_column`")
        return epoch_hours_diff(ts_expr(d.column, dialect), ts_expr(d.end_column, dialect), dialect)
    if t == "after_hours":
        if not (0 <= d.start_hour <= 24 and 0 <= d.end_hour <= 24 and d.start_hour < d.end_hour):
            raise InvalidInput("after_hours requires 0 <= start_hour < end_hour <= 24")
        ts = ts_expr(d.column, dialect)
        h = hour_expr(ts, dialect)
        dow = dow_expr(ts.copy(), dialect)
        outside = or_all([
            exp.LT(this=h, expression=num(d.start_hour)),
            exp.GTE(this=h.copy(), expression=num(d.end_hour)),
            exp.In(this=dow, expressions=[num(0), num(6)]),
        ])
        return case([(is_null(col(d.column)), exp.Null()), (outside, num(1))], num(0))
    if t == "bucket":
        return bucket_exprs(d, dialect)[0]
    if t == "equals":
        if d.value is None:
            raise InvalidInput("equals derivation requires `value`")
        c = col(d.column)
        if isinstance(d.value, bool):
            return case([(is_null(c), exp.Null()),
                         (exp.EQ(this=is_true_expr(c.copy(), dialect), expression=num(1 if d.value else 0)), num(1))],
                        num(0))
        return case([(is_null(c), exp.Null()), (exp.EQ(this=c.copy(), expression=lit(d.value, dialect)), num(1))], num(0))
    if t == "is_true":
        return is_true_expr(col(d.column), dialect)
    if t == "date_trunc":
        return trunc_expr(ts_expr(d.column, dialect), d.grain or "month", dialect)
    if t == "hour_of_day":
        return hour_expr(ts_expr(d.column, dialect), dialect)
    if t == "day_of_week":
        return dow_expr(ts_expr(d.column, dialect), dialect)
    raise InvalidInput(f"unsupported derivation type {t!r}")


def time_period_expr(d: Derivation, dialect: str, default_grain: str = "month") -> exp.Expression:
    """A spec's `time`: a date_trunc derivation is used as-is; a raw column is truncated to `grain` or month."""
    if d.type == "date_trunc":
        return derive(d, dialect)
    if d.type == "column":
        return trunc_expr(ts_expr(d.column, dialect), d.grain or default_grain, dialect)
    raise InvalidInput("`time` must be a column or date_trunc derivation")


def describe(d: Derivation | None) -> str:
    if d is None:
        return "count(*)"
    if d.label:
        return d.label
    if d.type == "column":
        return d.column
    if d.type == "duration_hours":
        return f"hours({d.column}->{d.end_column})"
    if d.type == "equals":
        return f"{d.column}={d.value!r}"
    if d.type == "date_trunc":
        return f"{d.grain or 'month'}({d.column})"
    return f"{d.type}({d.column})"


# --------------------------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------------------------
_OPS = {"=": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE}


def filter_expr(f: Filter, dialect: str) -> exp.Expression:
    c = col(f.column)
    if f.op == "is null":
        return is_null(c)
    if f.op == "is not null":
        return not_null(c)
    if f.op in ("in", "not in"):
        vals = f.value if isinstance(f.value, list | tuple | set) else [f.value]
        vals = [v for v in vals]
        if not vals:
            raise InvalidInput(f"filter {f.column} {f.op} needs at least one value")
        e = exp.In(this=c, expressions=[lit(v, dialect) for v in vals])
        return exp.Not(this=e) if f.op == "not in" else e
    if f.value is None:
        raise InvalidInput(f"filter {f.column} {f.op} requires a value (use 'is null' for NULL)")
    return _OPS[f.op](this=c, expression=lit(f.value, dialect))


def where_clause(filters: list[Filter], dialect: str) -> exp.Expression | None:
    return and_all([filter_expr(f, dialect) for f in filters])


def _qualify(e: exp.Expression, table_alias: str | None) -> exp.Expression:
    if not table_alias:
        return e

    def q(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table:
            node = node.copy()
            node.set("table", ident(table_alias))
        return node

    return e.transform(q)


def render_derivation(d: Derivation, dialect: str, *, table_alias: str | None = None) -> str:
    """SQL text of one derivation's value expression (quoted identifiers, dialect-correct).

    Boolean derivations render as 1/0/NULL; bucket renders its (string-sortable) label CASE. With
    `table_alias`, every column is qualified (``"t"."created_at"``) so the expression can be used in
    ``SELECT t.*, <expr> AS ... FROM <asset> AS t``.
    """
    dialect = _check_dialect(dialect)
    return _qualify(derive(d, dialect), table_alias).sql(dialect=dialect)


def render_filter(f: Filter, dialect: str, *, table_alias: str | None = None) -> str:
    """SQL text of one filter as a boolean expression (values escaped as literals)."""
    dialect = _check_dialect(dialect)
    return _qualify(filter_expr(f, dialect), table_alias).sql(dialect=dialect)


# --------------------------------------------------------------------------------------------
# query assembly
# --------------------------------------------------------------------------------------------
def base_select(spec: AnalysisSpec, dialect: str, cols: list[tuple[str, exp.Expression]]) -> exp.Select:
    q = exp.select(*[e.as_(ident(a)) for a, e in cols]).from_(table(spec.asset))
    w = where_clause(spec.filters, dialect)
    if w is not None:
        q = q.where(w)
    return q


def subquery(q: exp.Select, alias: str) -> exp.Subquery:
    return exp.Subquery(this=q, alias=exp.TableAlias(this=ident(alias)))


def ref(alias: str) -> exp.Column:
    return col(alias)


def _qc(alias: str, tbl: str) -> exp.Column:
    """Table-qualified column reference (`"s"."segment"`)."""
    return exp.column(alias, table=tbl, quoted=True)


def _hash_order(aliases: list[str], rn_alias: str, dialect: str, tbl: str) -> exp.Expression:
    parts: list[exp.Expression] = []
    for a in [*aliases, rn_alias]:
        if parts:
            parts.append(lit("|"))
        parts.append(exp.Coalesce(this=cast(_qc(a, tbl), "text", dialect), expressions=[lit("~")]))
    concat = exp.Concat(expressions=parts) if dialect != "tsql" else fn("CONCAT", *parts)
    if dialect == "tsql":
        return fn("HASHBYTES", lit("MD5"), concat)
    return exp.MD5(this=concat)


def sample_query(spec: AnalysisSpec, dialect: str, cols: list[tuple[str, exp.Expression]], sample_rows: int) -> exp.Select:
    """Deterministic bounded sample of derived columns with NULL-exclusion counts.

    inner : derived columns under the spec filters
    middle: + row number among identical tuples, total row count, count of rows with any NULL
    outer : non-NULL rows ordered by md5(values || row_number) LIMIT sample_rows

    Every column in the middle/outer stages is qualified with its derived-table alias ("d" / "s"):
    the gateway rejects unqualified names inside ORDER BY *expressions* (Postgres would resolve them
    to input columns rather than select-list aliases).
    """
    aliases = [a for a, _ in cols]
    inner = base_select(spec, dialect, cols)
    any_null = or_all([is_null(_qc(a, "d")) for a in aliases])
    rn = exp.Window(this=exp.RowNumber(), partition_by=[_qc(a, "d") for a in aliases],
                    order=exp.Order(expressions=[exp.Ordered(this=_qc(aliases[0], "d"), nulls_first=dialect == "tsql")]))
    total = exp.Window(this=count_star())
    excluded = exp.Window(this=exp.Sum(this=case([(any_null, num(1))], num(0))))
    middle = exp.select(*[_qc(a, "d").as_(ident(a)) for a in aliases], rn.as_(ident("_rn")),
                        total.as_(ident("_rows_total")), excluded.as_(ident("_rows_excluded"))).from_(subquery(inner, "d"))
    outer = (exp.select(*[_qc(a, "s").as_(ident(a)) for a in aliases], _qc("_rows_total", "s").as_(ident("_rows_total")),
                        _qc("_rows_excluded", "s").as_(ident("_rows_excluded")))
             .from_(subquery(middle, "s"))
             .where(and_all([not_null(_qc(a, "s")) for a in aliases]))
             .order_by(_hash_order(aliases, "_rn", dialect, "s"), *[_qc(a, "s") for a in aliases])
             .limit(int(sample_rows)))
    return outer


def as_double(e: exp.Expression, dialect: str) -> exp.Expression:
    return cast(e, "double", dialect)


def percentile_cont(e: exp.Expression, q: float, dialect: str) -> exp.Expression:
    """PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY e). An aggregate on postgres/duckdb; on tsql it is
    window-only, so callers wrap it in OVER () there. NULL ordering is set to each engine's default
    so no extra sort key is generated (PERCENTILE_CONT accepts exactly one)."""
    return exp.WithinGroup(this=exp.PercentileCont(this=num(q)),
                           expression=exp.Order(expressions=[exp.Ordered(this=e, nulls_first=dialect == "tsql")]))


def median(e: exp.Expression) -> exp.Expression:
    return percentile_cont(e, 0.5, "postgres")


AGGREGATES = ("count", "sum", "avg", "median")


def aggregate_query(asset: str, dialect: str, *, dimensions: list[tuple[str, Derivation]],
                    measure: tuple[str, str, Derivation | None], order: str = "dimensions",
                    limit: int | None = None) -> str:
    """One grouped aggregate over one table, for Ask's rules rung: ``SELECT <dims>, <AGG>(<measure>)
    FROM <asset> GROUP BY <dims>``, ordered by the dimensions (a series reads in time order) or by
    the measure descending then the dimensions (`order="measure_desc"`, a ranking; ties are broken
    by the dimensions so `limit` is deterministic). `measure` is (alias, aggregate, derivation);
    COUNT(*) takes no derivation. Identifiers are quoted and every expression is a sqlglot node."""
    dialect = _check_dialect(dialect)
    alias, agg, d = measure
    if agg not in AGGREGATES:
        raise InvalidInput(f"unsupported aggregate {agg!r}; expected one of {AGGREGATES}")
    if agg == "count":
        m: exp.Expression = count_star()
    else:
        if d is None:
            raise InvalidInput(f"{agg} needs a column")
        value = derive(d, dialect)
        if agg == "median":
            if dialect == "tsql":
                raise InvalidInput("median is a window function on tsql; not supported in a grouped aggregate")
            m = percentile_cont(value, 0.5, dialect)
        else:
            m = (exp.Sum if agg == "sum" else exp.Avg)(this=value)
    cols = [(a, derive(dd, dialect)) for a, dd in dimensions]
    q = exp.select(*[e.as_(ident(a)) for a, e in cols], m.as_(ident(alias))).from_(table(asset))
    if cols:
        q = q.group_by(*[e.copy() for _, e in cols])
    dims_order = [exp.Ordered(this=col(a), nulls_first=dialect == "tsql") for a, _ in cols]
    if order == "measure_desc":
        q = q.order_by(exp.Ordered(this=col(alias), desc=True), *dims_order)
    elif dims_order:
        q = q.order_by(*dims_order)
    if limit is not None:
        q = q.limit(int(limit))
    return q.sql(dialect=dialect)


def segment_cols(spec: AnalysisSpec, dialect: str) -> tuple[list[tuple[str, exp.Expression]], bool]:
    if spec.segment is None:
        raise InvalidInput(f"{spec.method} requires a `segment`")
    if spec.segment.type == "bucket":
        lab, order = bucket_exprs(spec.segment, dialect)
        return [("segment", lab), ("segment_order", order)], True
    return [("segment", derive(spec.segment, dialect))], False


def require(spec: AnalysisSpec, what: str) -> None:
    if getattr(spec, what) is None:
        raise InvalidInput(f"{spec.method} requires `{what}`")


def compile_spec(spec: AnalysisSpec, dialect: str, *, purpose: str = "primary", sample_rows: int = 50000) -> CompiledQuery:
    """Compile one query of `spec` for `dialect`.

    The spec's method (a registered `analystos.methods` plugin) builds the statement from the
    primitives above; `purpose` is one of the method's `purposes` ("primary" for every method).
    Returns a `CompiledQuery` whose `columns` maps roles (segment, outcome, n, positives, ...) to
    output column aliases.
    """
    from analystos import methods

    dialect = _check_dialect(dialect)
    method = methods.get(spec.method)
    if purpose not in method.purposes:
        raise InvalidInput(f"method {spec.method} has no {purpose!r} query (available: {method.purposes})")
    if sample_rows <= 0:
        raise InvalidInput("sample_rows must be positive")
    return method.compile(spec, dialect, purpose=purpose, sample_rows=sample_rows)


def compile_all(spec: AnalysisSpec, dialect: str, *, sample_rows: int = 50000) -> dict[str, CompiledQuery]:
    return {p: compile_spec(spec, dialect, purpose=p, sample_rows=sample_rows) for p in METHOD_PURPOSES[spec.method]}


class _MethodPurposes(Mapping[str, tuple[str, ...]]):
    """Which queries exist for which method, read from the method registry (never hand-kept)."""

    def __getitem__(self, name: str) -> tuple[str, ...]:
        from analystos import methods

        return methods.get(name).purposes

    def __iter__(self) -> Iterator[str]:
        from analystos import methods

        return iter(methods.names())

    def __len__(self) -> int:
        from analystos import methods

        return len(methods.names())


METHOD_PURPOSES: Mapping[str, tuple[str, ...]] = _MethodPurposes()


def parses(sql: str, dialect: str) -> bool:
    """True when sqlglot can parse `sql` in `dialect` (what the gateway's validator does first)."""
    try:
        return sqlglot.parse_one(sql, read=_check_dialect(dialect)) is not None
    except sqlglot.errors.ParseError:
        return False
