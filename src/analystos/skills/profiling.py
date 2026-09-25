"""Pushdown profiling of one table through the governed gateway.

Round trips per table are bounded:

1. one wide aggregate: row count, and per column non-null / distinct counts, min / max, mean /
   stddev (numeric), true count (boolean), text lengths; percentiles (p01..p99) inline on
   postgres/duckdb where PERCENTILE_CONT is an aggregate;
2. tsql only, if there are numeric columns: one `SELECT TOP 1 PERCENTILE_CONT(..) OVER ()` query;
3. if there are numeric columns: one UNION ALL query with a 20-bin histogram per numeric column plus
   the counts outside the IQR fences (outlier candidates);
4. one top-10 query per low-cardinality categorical/boolean column (at most 12 columns);
5. one monthly-count query per datetime column.

Total <= 3 + #datetime columns + min(#categorical columns, 12).
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlglot import exp

from analystos.skills.base import RunSQL
from analystos.skills.sqlbuild import (
    _check_dialect,
    cast,
    col,
    count_star,
    ident,
    is_true_expr,
    lit,
    not_null,
    num,
    percentile_cont,
    table,
    to_sql,
    trunc_expr,
)

MAX_CATEGORICAL_TOPN = 12
TOP_N = 10
HIST_BINS = 20
CATEGORICAL_MAX_DISTINCT = 200
CATEGORICAL_MAX_RATIO = 0.05
PERCENTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)
ID_NAME = re.compile(r"(^id$|_id$|^sys_id$|^number$|^uuid$|^guid$|_key$|^key$|_uuid$|_guid$)", re.I)


class ColumnProfile(BaseModel):
    name: str
    data_type: str
    type_family: str  # numeric | datetime | boolean | text
    semantic_type: str  # id | numeric | categorical | boolean | datetime | text
    is_key: bool = False
    non_null: int = 0
    null_count: int = 0
    null_rate: float = 0.0
    distinct: int = 0
    distinct_ratio: float = 0.0
    min: Any = None
    max: Any = None
    mean: float | None = None
    stddev: float | None = None
    percentiles: dict[str, float | None] = Field(default_factory=dict)
    true_count: int | None = None
    avg_length: float | None = None
    max_length: int | None = None
    top_values: list[dict[str, Any]] = Field(default_factory=list)
    monthly_counts: list[dict[str, Any]] = Field(default_factory=list)
    histogram: list[dict[str, Any]] = Field(default_factory=list)
    outliers: dict[str, Any] = Field(default_factory=dict)


class AssetProfile(BaseModel):
    asset: str
    dialect: str
    row_count: int
    columns: list[ColumnProfile]
    candidate_keys: list[dict[str, Any]] = Field(default_factory=list)
    query_ids: list[str] = Field(default_factory=list)
    sql: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def n_queries(self) -> int:
        return len(self.sql)

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.columns if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["n_queries"] = self.n_queries
        return d


def type_family(data_type: str) -> str:
    t = (data_type or "").lower()
    if t in ("bool", "boolean", "bit") or t.startswith("bool"):
        return "boolean"
    if any(k in t for k in ("timestamp", "datetime", "date", "time")) and "interval" not in t:
        return "datetime"
    if any(k in t for k in ("int", "numeric", "decimal", "float", "double", "real", "money", "number", "hugeint")):
        return "numeric"
    return "text"


def infer_semantic_type(name: str, family: str, *, row_count: int, non_null: int, distinct: int,
                        avg_length: float | None = None, min_value: Any = None, max_value: Any = None,
                        is_key: bool = False, references: bool = False, max_length: int | None = None) -> str:
    """Rule-based semantic type from name, physical type family and profile statistics.

    Declared keys and reference (foreign-key) columns are `id`; so are id-named columns that are
    (nearly) unique or end in `_id`, and fixed-width 32-character text (ServiceNow sys_id shape).
    """
    ratio = distinct / non_null if non_null else 0.0
    idname = bool(ID_NAME.search(name))
    if family == "boolean":
        return "boolean"
    if family == "datetime":
        return "datetime"
    if is_key or references:
        return "id"
    if family == "numeric":
        if distinct <= 2 and non_null > 0 and _num(min_value) in (0, 1, None) and _num(max_value) in (0, 1, None):
            return "boolean"
        if idname and (ratio >= 0.9 or name.lower().endswith("_id")):
            return "id"
        return "numeric"
    # text
    if idname and (ratio >= 0.9 or name.lower().endswith("_id")):
        return "id"
    if non_null >= 20 and ratio >= 0.95 and (avg_length or 0) <= 40:
        return "id"
    if avg_length is not None and max_length == 32 and abs(avg_length - 32) < 1e-9 and distinct > 2:
        return "id"
    if avg_length is not None and avg_length > 60:
        return "text"
    if distinct <= CATEGORICAL_MAX_DISTINCT or ratio <= CATEGORICAL_MAX_RATIO:
        return "categorical"
    return "text"


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int | float | Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, _dt.datetime | _dt.date | _dt.time):
        return v.isoformat()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def _pkey(q: float) -> str:
    return f"p{int(round(q * 100)):02d}"


class _Q:
    def __init__(self, run_sql: RunSQL, asset: str):
        self.run_sql, self.asset = run_sql, asset
        self.ids: list[str] = []
        self.sql: list[str] = []

    def __call__(self, q: exp.Expression, dialect: str, purpose: str, max_rows: int | None = None) -> list[dict[str, Any]]:
        sql = to_sql(q, dialect)
        res = self.run_sql(sql, purpose=f"profile.{purpose}", max_rows=max_rows)
        self.ids.append(res.query_id)
        self.sql.append(sql)
        return [{k.lower(): v for k, v in r.items()} for r in res.records()]


def profile_asset(run_sql: RunSQL, asset: str, columns: list[dict[str, Any]], *, top_n: int = TOP_N,
                  hist_bins: int = HIST_BINS) -> AssetProfile:
    """Profile `asset` with a bounded number of pushdown queries (see module docstring)."""
    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    q = _Q(run_sql, asset)
    t = table(asset)
    fams = {c["name"]: type_family(c.get("data_type", "")) for c in columns}

    # ---- 1. wide aggregate ----------------------------------------------------------------
    sel: list[exp.Expression] = [count_star().as_(ident("row_count"))]
    for i, c in enumerate(columns):
        name, fam = c["name"], fams[c["name"]]
        x = col(name)
        sel += [exp.Count(this=x).as_(ident(f"c{i}_nn")),
                exp.Count(this=exp.Distinct(expressions=[x.copy()])).as_(ident(f"c{i}_nd"))]
        if fam == "numeric":
            xd = cast(x.copy(), "double", dialect)
            sel += [exp.Min(this=x.copy()).as_(ident(f"c{i}_min")), exp.Max(this=x.copy()).as_(ident(f"c{i}_max")),
                    exp.Avg(this=xd).as_(ident(f"c{i}_mean")),
                    exp.Stddev(this=xd.copy()).as_(ident(f"c{i}_std"))]
            if dialect != "tsql":
                sel += [percentile_cont(xd.copy(), p, dialect).as_(ident(f"c{i}_{_pkey(p)}")) for p in PERCENTILES]
        elif fam == "datetime":
            sel += [exp.Min(this=x.copy()).as_(ident(f"c{i}_min")), exp.Max(this=x.copy()).as_(ident(f"c{i}_max"))]
        elif fam == "boolean":
            sel += [exp.Sum(this=is_true_expr(x.copy(), dialect)).as_(ident(f"c{i}_true"))]
        else:
            ln = exp.Length(this=cast(x.copy(), "text", dialect))
            sel += [exp.Avg(this=cast(ln, "double", dialect)).as_(ident(f"c{i}_avglen")),
                    exp.Max(this=ln.copy()).as_(ident(f"c{i}_maxlen"))]
    wide = q(exp.select(*sel).from_(t), dialect, "wide", max_rows=1)[0]
    n = int(wide["row_count"] or 0)

    profs: list[ColumnProfile] = []
    for i, c in enumerate(columns):
        name, fam = c["name"], fams[c["name"]]
        nn = int(wide.get(f"c{i}_nn") or 0)
        nd = int(wide.get(f"c{i}_nd") or 0)
        cp = ColumnProfile(name=name, data_type=str(c.get("data_type", "")), type_family=fam, semantic_type="text",
                           is_key=bool(c.get("is_key")), non_null=nn, null_count=n - nn,
                           null_rate=round((n - nn) / n, 6) if n else 0.0, distinct=nd,
                           distinct_ratio=round(nd / nn, 6) if nn else 0.0)
        if fam in ("numeric", "datetime"):
            cp.min, cp.max = _jsonable(wide.get(f"c{i}_min")), _jsonable(wide.get(f"c{i}_max"))
        if fam == "numeric":
            cp.mean, cp.stddev = _num(wide.get(f"c{i}_mean")), _num(wide.get(f"c{i}_std"))
            if dialect != "tsql":
                cp.percentiles = {_pkey(p): _num(wide.get(f"c{i}_{_pkey(p)}")) for p in PERCENTILES}
        if fam == "boolean":
            cp.true_count = int(wide.get(f"c{i}_true") or 0)
        if fam == "text":
            cp.avg_length = _num(wide.get(f"c{i}_avglen"))
            ml = wide.get(f"c{i}_maxlen")
            cp.max_length = int(ml) if ml is not None else None
        cp.semantic_type = infer_semantic_type(name, fam, row_count=n, non_null=nn, distinct=nd, avg_length=cp.avg_length,
                                               min_value=cp.min, max_value=cp.max, is_key=cp.is_key,
                                               references=bool(c.get("references")), max_length=cp.max_length)
        profs.append(cp)
    by_name = {p.name: p for p in profs}
    numeric = [p for p in profs if p.type_family == "numeric" and p.semantic_type in ("numeric",) and p.non_null > 0]

    # ---- 2. tsql percentiles ---------------------------------------------------------------
    if dialect == "tsql" and numeric:
        sel = []
        for j, p in enumerate(numeric):
            xd = cast(col(p.name), "double", dialect)
            sel += [exp.Window(this=percentile_cont(xd.copy(), qq, dialect)).as_(ident(f"n{j}_{_pkey(qq)}"))
                    for qq in PERCENTILES]
        row = q(exp.select(*sel).from_(table(asset)).limit(1), dialect, "percentiles", max_rows=1)
        if row:
            for j, p in enumerate(numeric):
                p.percentiles = {_pkey(qq): _num(row[0].get(f"n{j}_{_pkey(qq)}")) for qq in PERCENTILES}

    # ---- 3. histograms + IQR outlier counts ------------------------------------------------
    parts: list[exp.Select] = []
    for p in numeric:
        lo, hi = _num(p.min), _num(p.max)
        if lo is None or hi is None:
            continue
        x = cast(col(p.name), "double", dialect)
        if hi > lo:
            bins = int(min(hist_bins, max(p.distinct, 1)))
            width = (hi - lo) / bins
            b = exp.Case().when(exp.GTE(this=x.copy(), expression=num(hi)), num(bins - 1)).else_(
                exp.Floor(this=exp.Div(this=exp.Paren(this=exp.Sub(this=x.copy(), expression=num(lo))), expression=num(width))))
            p.histogram = [{"bin": k, "low": lo + k * width, "high": lo + (k + 1) * width, "count": 0} for k in range(bins)]
        else:
            b = num(0)
            p.histogram = [{"bin": 0, "low": lo, "high": hi, "count": 0}]
        inner = exp.select(b.as_(ident("b"))).from_(table(asset)).where(not_null(col(p.name)))
        parts.append(exp.select(lit(p.name).as_(ident("column_name")), exp.column("b", quoted=True).as_(ident("bin")),
                                count_star().as_(ident("n")))
                     .from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=ident("h"))))
                     .group_by(exp.column("b", quoted=True)))
        q1, q3 = p.percentiles.get("p25"), p.percentiles.get("p75")
        if q1 is not None and q3 is not None:
            iqr = q3 - q1
            lf, hf = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            p.outliers = {"method": "iqr_1.5", "low_fence": lf, "high_fence": hf, "low_count": 0, "high_count": 0}
            ob = exp.Case().when(exp.LT(this=x.copy(), expression=num(lf)), num(-1)).else_(num(-2))
            cond = exp.Or(this=exp.LT(this=x.copy(), expression=num(lf)), expression=exp.GT(this=x.copy(), expression=num(hf)))
            inner2 = exp.select(ob.as_(ident("b"))).from_(table(asset)).where(cond)
            parts.append(exp.select(lit(p.name).as_(ident("column_name")), exp.column("b", quoted=True).as_(ident("bin")),
                                    count_star().as_(ident("n")))
                         .from_(exp.Subquery(this=inner2, alias=exp.TableAlias(this=ident("o"))))
                         .group_by(exp.column("b", quoted=True)))
    if parts:
        u: exp.Expression = parts[0]
        for part in parts[1:]:
            u = exp.union(u, part, distinct=False)
        for r in q(u, dialect, "histograms"):
            p = by_name[r["column_name"]]
            k, cnt = int(r["bin"]), int(r["n"])
            if k == -1:
                p.outliers["low_count"] = cnt
            elif k == -2:
                p.outliers["high_count"] = cnt
            elif 0 <= k < len(p.histogram):
                p.histogram[k]["count"] += cnt
        for p in numeric:
            for h in p.histogram:
                h["low"], h["high"] = _jsonable(h["low"]), _jsonable(h["high"])

    # ---- 4. top values for low-cardinality categorical/boolean columns -----------------------
    cats = [p for p in profs if p.semantic_type in ("categorical", "boolean") and p.non_null > 0]
    cats.sort(key=lambda p: p.distinct)
    for p in cats[:MAX_CATEGORICAL_TOPN]:
        x = col(p.name)
        rows = q(exp.select(x.as_(ident("value")), count_star().as_(ident("n"))).from_(table(asset))
                 .where(not_null(x.copy())).group_by(x.copy())
                 .order_by(exp.Ordered(this=exp.column("n", quoted=True), desc=True), x.copy()).limit(top_n),
                 dialect, f"top_values.{p.name}", max_rows=top_n)
        p.top_values = [{"value": _jsonable(r["value"]), "count": int(r["n"]),
                         "share": round(int(r["n"]) / p.non_null, 6) if p.non_null else None} for r in rows]
    if len(cats) > MAX_CATEGORICAL_TOPN:
        skipped = [p.name for p in cats[MAX_CATEGORICAL_TOPN:]]
        warnings = [f"top values skipped for {len(skipped)} categorical column(s) beyond the cap of "
                    f"{MAX_CATEGORICAL_TOPN}: {skipped}"]
    else:
        warnings = []

    # ---- 5. monthly counts per datetime column ----------------------------------------------
    for p in profs:
        if p.semantic_type != "datetime" or p.non_null == 0:
            continue
        m = trunc_expr(cast(col(p.name), "timestamp", dialect), "month", dialect)
        inner = exp.select(m.as_(ident("month"))).from_(table(asset)).where(not_null(col(p.name)))
        rows = q(exp.select(exp.column("month", quoted=True), count_star().as_(ident("n")))
                 .from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=ident("m"))))
                 .group_by(exp.column("month", quoted=True)).order_by(exp.column("month", quoted=True)).limit(1200),
                 dialect, f"monthly.{p.name}", max_rows=1200)
        p.monthly_counts = [{"month": _jsonable(r["month"]), "count": int(r["n"])} for r in rows]

    # ---- candidate keys ----------------------------------------------------------------------
    keys = []
    for p in profs:
        hinted = p.is_key or (p.semantic_type == "id" and bool(ID_NAME.search(p.name)) and not p.name.lower().endswith("_id"))
        unique = p.non_null > 0 and p.distinct == p.non_null
        if hinted or (unique and p.null_count == 0 and p.semantic_type in ("id",)):
            keys.append({"column": p.name, "declared": p.is_key, "unique": unique and p.null_count == 0,
                         "duplicate_rows": p.non_null - p.distinct, "null_count": p.null_count,
                         "distinct": p.distinct, "non_null": p.non_null})
    return AssetProfile(asset=asset, dialect=dialect, row_count=n, columns=profs, candidate_keys=keys, query_ids=q.ids,
                        sql=q.sql, warnings=warnings)
