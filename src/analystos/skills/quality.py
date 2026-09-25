"""Deterministic data-quality checks for one table.

Checks (codes):
  high_null_rate        null share >= 20% (info), >= 50% (warning), 100% (warning, "all null")
  constant_column       exactly one distinct non-null value
  duplicate_key         a declared/candidate key column has repeated values (critical for declared keys
                        and `id` / `sys_id` / `number`)
  orphan_reference      FK values with no matching parent row, for the given relationships
                        (critical when > 5% of non-null references)
  temporal_order        end < start for lifecycle pairs (opened_at/resolved_at/closed_at,
                        start_*/end_*, created/updated...)
  future_timestamp      timestamps later than `now` + 1 day (planning columns such as due/expected/
                        planned/scheduled/target/expiry/end dates are skipped)
  case_variant_category categorical values equal after lower()/trim() but spelled differently

Profile-derived issues cost no queries; the SQL checks use one query per duplicate key column, one
temporal query per table, one case-variant count query per table plus one detail query per affected
column, and one query per orphan relationship. Each issue carries the SQL that produced its numbers.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlglot import exp

from analystos.skills.base import RunSQL
from analystos.skills.profiling import AssetProfile
from analystos.skills.sqlbuild import (
    _check_dialect,
    and_all,
    case,
    cast,
    col,
    count_star,
    epoch_hours_diff,
    ident,
    is_null,
    lit,
    not_null,
    num,
    table,
    to_sql,
)

Severity = Literal["info", "warning", "critical"]

NULL_INFO, NULL_WARN = 0.2, 0.5
ORPHAN_CRITICAL = 0.05
FUTURE_TOLERANCE = _dt.timedelta(days=1)
PLANNING_NAME = re.compile(r"(due|expect|plan|schedul|estimat|target|valid_to|expir|end_date|next_|deadline|renew)", re.I)
STRICT_KEY_NAMES = {"id", "sys_id", "number"}

KNOWN_PAIRS = [
    ("opened_at", "resolved_at"), ("opened_at", "closed_at"), ("resolved_at", "closed_at"),
    ("opened_at", "work_start"), ("work_start", "work_end"), ("sys_created_on", "sys_updated_on"),
    ("sys_created_on", "closed_at"), ("created_at", "updated_at"), ("created_at", "closed_at"),
    ("created_at", "resolved_at"), ("created", "updated"), ("start_date", "end_date"), ("start_time", "end_time"),
    ("started_at", "ended_at"), ("started_at", "completed_at"), ("start", "end"), ("begin_date", "end_date"),
    ("valid_from", "valid_to"), ("order_date", "ship_date"), ("ship_date", "delivery_date"),
]


class QualityIssue(BaseModel):
    code: str
    severity: Severity
    asset: str
    column: str | None = None
    message: str
    metric: dict[str, Any] = Field(default_factory=dict)
    sql: str = ""


def _pct(k: int | float, n: int | float) -> str:
    return f"{(100.0 * k / n):.2f}%" if n else "n/a"


def temporal_pairs(datetime_columns: list[str]) -> list[tuple[str, str]]:
    """(start, end) pairs among `datetime_columns`: known lifecycle pairs + start/begin -> end renames."""
    cols = {c.lower(): c for c in datetime_columns}
    pairs: list[tuple[str, str]] = []
    for a, b in KNOWN_PAIRS:
        if a in cols and b in cols:
            pairs.append((cols[a], cols[b]))
    for lc, c in cols.items():
        for s, e in (("start", "end"), ("begin", "end"), ("from", "to")):
            if s in lc:
                other = lc.replace(s, e)
                if other in cols and (c, cols[other]) not in pairs and other != lc:
                    pairs.append((c, cols[other]))
    return pairs


def _rel_get(r: Any, k: str) -> Any:
    return r.get(k) if isinstance(r, Mapping) else getattr(r, k, None)


class _Q:
    def __init__(self, run_sql: RunSQL, dialect: str):
        self.run_sql, self.dialect = run_sql, dialect

    def __call__(self, q: exp.Expression, purpose: str, max_rows: int | None = None) -> tuple[list[dict[str, Any]], str]:
        sql = to_sql(q, self.dialect)
        res = self.run_sql(sql, purpose=f"quality.{purpose}", max_rows=max_rows)
        return [{k.lower(): v for k, v in r.items()} for r in res.records()], sql


def check_quality(run_sql: RunSQL, asset: str, profile: AssetProfile, relationships: list[Any] | None = None, *,
                  now: _dt.datetime | None = None) -> list[QualityIssue]:
    """Run the checks listed in the module docstring; returns issues ordered by severity."""
    dialect = _check_dialect(getattr(run_sql, "dialect", "duckdb"))
    q = _Q(run_sql, dialect)
    now = now or _dt.datetime.now(_dt.UTC).replace(tzinfo=None)
    n = profile.row_count
    issues: list[QualityIssue] = []

    # ---- profile-derived ----------------------------------------------------------------------
    for c in profile.columns:
        if n and c.null_rate >= NULL_INFO:
            if c.null_count == n:
                sev: Severity = "warning"
                msg = f"{c.name} is NULL in all {n} rows"
            else:
                sev = "warning" if c.null_rate >= NULL_WARN else "info"
                msg = f"{c.name} is NULL in {c.null_count} of {n} rows ({_pct(c.null_count, n)})"
            issues.append(QualityIssue(code="high_null_rate", severity=sev, asset=asset, column=c.name, message=msg,
                                       metric={"null_count": c.null_count, "rows": n, "null_rate": c.null_rate,
                                               "source": "profile"}))
        if c.non_null > 0 and c.distinct == 1:
            val = c.top_values[0]["value"] if c.top_values else c.min
            issues.append(QualityIssue(code="constant_column", severity="info", asset=asset, column=c.name,
                                       message=f"{c.name} has a single distinct value ({val!r}) across {c.non_null} "
                                               f"non-null rows; it carries no analytical signal",
                                       metric={"distinct": 1, "value": val, "non_null": c.non_null, "source": "profile"}))

    # ---- duplicate keys ------------------------------------------------------------------------
    for k in profile.candidate_keys:
        if k.get("duplicate_rows", 0) <= 0:
            continue
        name = k["column"]
        x = col(name)
        inner = (exp.select(x.as_(ident("v")), count_star().as_(ident("cnt"))).from_(table(asset))
                 .where(not_null(x.copy())).group_by(x.copy()).having(exp.GT(this=count_star(), expression=num(1))))
        agg = exp.select(count_star().as_(ident("dup_values")), exp.Sum(this=exp.column("cnt", quoted=True)).as_(ident("dup_rows")),
                         exp.Max(this=exp.column("v", quoted=True)).as_(ident("example_max")),
                         exp.Min(this=exp.column("v", quoted=True)).as_(ident("example_min"))
                         ).from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=ident("d"))))
        rows, sql = q(agg, f"duplicate_key.{name}", max_rows=1)
        r = rows[0] if rows else {}
        dv, dr = int(r.get("dup_values") or 0), int(r.get("dup_rows") or 0)
        if dv == 0:
            continue
        sev = "critical" if (k.get("declared") or name.lower() in STRICT_KEY_NAMES) else "warning"
        examples = sorted({str(e) for e in (r.get("example_min"), r.get("example_max")) if e is not None})
        issues.append(QualityIssue(code="duplicate_key", severity=sev, asset=asset, column=name,
                                   message=f"{dv} value(s) of key column {name} occur more than once, affecting {dr} rows "
                                           f"({_pct(dr, n)} of {n}); e.g. {', '.join(examples)}",
                                   metric={"duplicate_values": dv, "rows_affected": dr, "rows": n,
                                           "extra_rows": k.get("duplicate_rows"), "examples": examples}, sql=sql))

    # ---- temporal order + future timestamps (one query) ---------------------------------------
    dts = [c.name for c in profile.columns if c.semantic_type == "datetime" and c.non_null > 0]
    pairs = temporal_pairs(dts)
    fut_cols = [c for c in dts if not PLANNING_NAME.search(c)]
    if pairs or fut_cols:
        sel: list[exp.Expression] = []
        for i, (a, b) in enumerate(pairs):
            ta, tb = cast(col(a), "timestamp", dialect), cast(col(b), "timestamp", dialect)
            sel.append(exp.Sum(this=case([(exp.LT(this=tb, expression=ta), num(1))], num(0))).as_(ident(f"v{i}")))
            both = and_all([not_null(col(a)), not_null(col(b))])
            sel.append(exp.Sum(this=case([(both, num(1))], num(0))).as_(ident(f"b{i}")))
            sel.append(exp.Min(this=epoch_hours_diff(ta.copy(), tb.copy(), dialect)).as_(ident(f"w{i}")))
        limit_ts = cast(lit((now + FUTURE_TOLERANCE).replace(microsecond=0)), "timestamp", dialect)
        for j, c in enumerate(fut_cols):
            sel.append(exp.Sum(this=case([(exp.GT(this=cast(col(c), "timestamp", dialect), expression=limit_ts.copy()), num(1))],
                                         num(0))).as_(ident(f"f{j}")))
            sel.append(exp.Max(this=col(c)).as_(ident(f"m{j}")))
        rows, sql = q(exp.select(*sel).from_(table(asset)), "temporal", max_rows=1)
        r = rows[0] if rows else {}
        for i, (a, b) in enumerate(pairs):
            v, both = int(r.get(f"v{i}") or 0), int(r.get(f"b{i}") or 0)
            if v:
                worst = r.get(f"w{i}")
                issues.append(QualityIssue(
                    code="temporal_order", severity="warning" if v / max(both, 1) < 0.05 else "critical", asset=asset,
                    column=b, message=f"{b} is earlier than {a} in {v} of {both} rows where both are set ({_pct(v, both)}); "
                                      f"worst gap {abs(float(worst)):.1f} hours" if worst is not None else
                                      f"{b} is earlier than {a} in {v} of {both} rows where both are set ({_pct(v, both)})",
                    metric={"violations": v, "rows_with_both": both, "start_column": a, "end_column": b,
                            "worst_gap_hours": float(worst) if worst is not None else None}, sql=sql))
        for j, c in enumerate(fut_cols):
            f = int(r.get(f"f{j}") or 0)
            if f:
                cp = profile.column(c)
                nn = cp.non_null if cp else n
                mx = r.get(f"m{j}")
                issues.append(QualityIssue(
                    code="future_timestamp", severity="warning", asset=asset, column=c,
                    message=f"{c} is in the future (after {now:%Y-%m-%d} + 1 day) in {f} of {nn} non-null rows "
                            f"({_pct(f, nn)}); latest {mx}",
                    metric={"future_rows": f, "non_null": nn, "reference_time": now.isoformat(),
                            "max": mx.isoformat() if hasattr(mx, "isoformat") else mx}, sql=sql))

    # ---- case-variant categories ----------------------------------------------------------------
    cats = [c for c in profile.columns if c.semantic_type == "categorical" and c.type_family == "text" and c.distinct > 1]
    if cats:
        sel = []
        for i, c in enumerate(cats):
            norm = exp.Lower(this=exp.Trim(this=col(c.name)))
            sel.append(exp.Count(this=exp.Distinct(expressions=[col(c.name)])).as_(ident(f"r{i}")))
            sel.append(exp.Count(this=exp.Distinct(expressions=[norm])).as_(ident(f"n{i}")))
        rows, sql = q(exp.select(*sel).from_(table(asset)), "case_variants", max_rows=1)
        r = rows[0] if rows else {}
        for i, c in enumerate(cats):
            raw, normd = int(r.get(f"r{i}") or 0), int(r.get(f"n{i}") or 0)
            if raw <= normd:
                continue
            norm = exp.Lower(this=exp.Trim(this=col(c.name)))
            dup_norm = (exp.select(norm.copy()).from_(table(asset)).where(not_null(col(c.name))).group_by(norm.copy())
                        .having(exp.GT(this=exp.Count(this=exp.Distinct(expressions=[col(c.name)])), expression=num(1))))
            detail = (exp.select(col(c.name).as_(ident("value")), count_star().as_(ident("n"))).from_(table(asset))
                      .where(exp.In(this=norm.copy(), query=exp.Subquery(this=dup_norm))).group_by(col(c.name))
                      .order_by(exp.Ordered(this=exp.column("n", quoted=True), desc=True), col(c.name)).limit(50))
            drows, dsql = q(detail, f"case_variants.{c.name}", max_rows=50)
            groups: dict[str, list[dict[str, Any]]] = {}
            for d in drows:
                groups.setdefault(str(d["value"]).strip().lower(), []).append({"value": d["value"], "count": int(d["n"])})
            minority = sum(sum(v["count"] for v in vs[1:]) for vs in groups.values())
            ex = "; ".join(" vs ".join(f"'{v['value']}' ({v['count']})" for v in vs) for vs in list(groups.values())[:3])
            issues.append(QualityIssue(
                code="case_variant_category", severity="warning", asset=asset, column=c.name,
                message=f"{c.name} has {raw} distinct values but only {normd} after case/whitespace normalisation; "
                        f"{minority} rows use a minority spelling ({_pct(minority, n)}): {ex}",
                metric={"distinct_raw": raw, "distinct_normalized": normd, "minority_rows": minority,
                        "variants": groups}, sql=dsql))

    # ---- orphan references ---------------------------------------------------------------------------
    for rel in relationships or []:
        if _rel_get(rel, "from_asset") != asset:
            continue
        fc, ta, tc = _rel_get(rel, "from_column"), _rel_get(rel, "to_asset"), _rel_get(rel, "to_column")
        if not (fc and ta and tc):
            continue
        parent = exp.select(col(tc).as_(ident("k"))).distinct().from_(table(ta)).where(not_null(col(tc)))
        f = exp.column(fc, table="f", quoted=True)
        pk = exp.column("k", table="p", quoted=True)
        orphan = is_null(pk.copy())
        qq = (exp.select(count_star().as_(ident("fk_rows")),
                         exp.Sum(this=case([(orphan, num(1))], num(0))).as_(ident("orphans")),
                         exp.Count(this=exp.Distinct(expressions=[case([(orphan.copy(), f.copy())], None)])).as_(ident("orphan_values")),
                         exp.Max(this=case([(orphan.copy(), f.copy())], None)).as_(ident("example")))
              .from_(table(asset, alias="f"))
              .join(exp.Subquery(this=parent, alias=exp.TableAlias(this=ident("p"))),
                    on=exp.EQ(this=f.copy(), expression=pk.copy()), join_type="left")
              .where(not_null(f.copy())))
        rows, sql = q(qq, f"orphans.{fc}", max_rows=1)
        r = rows[0] if rows else {}
        tot, orph = int(r.get("fk_rows") or 0), int(r.get("orphans") or 0)
        if orph:
            issues.append(QualityIssue(
                code="orphan_reference", severity="critical" if orph / max(tot, 1) > ORPHAN_CRITICAL else "warning",
                asset=asset, column=fc,
                message=f"{orph} of {tot} non-null {fc} values ({_pct(orph, tot)}; {int(r.get('orphan_values') or 0)} distinct) "
                        f"have no matching {ta}.{tc}; e.g. {r.get('example')!r}",
                metric={"orphan_rows": orph, "fk_rows": tot, "orphan_values": int(r.get("orphan_values") or 0),
                        "to_asset": ta, "to_column": tc, "example": r.get("example")}, sql=sql))

    rank = {"critical": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (rank[i.severity], i.code, i.column or ""))
    return issues
