"""Deterministic self-checks a step runs before REV (spec v4 §7, P7-04).

Each check reads what the step already has (its SQL, its result, earlier results of the same step, the
snapshot population record of P4-C12, declared relationships, recipe DQ gates, profiled column values);
none calls a model or runs a query. A failed check either proposes a **safe correction** — a rewrite
whose meaning is fixed by the data or by a declared key, so the corrected query answers the same
question — or flags the step:

* ``empty_result`` — no rows. Safe correction: a string literal compared with a column matches none of
  that column's profiled values but exactly one of them ignoring case (``'closed'`` vs ``'Closed'``).
* ``magnitude``    — the headline value is more than 10x away from the median of the step's earlier
  results (x100 / x0.01 is called out as a likely percent/fraction unit mistake). Flagged only.
* ``truncation``   — the result hit the row cap, or a staged table it read is an undeclared or cut
  sample (the P4-C12 ``representative_population`` record). Flagged only.
* ``grouping``     — the same group appears more than once, groups differ only in case or spacing, or
  the query groups by a column it does not show. Flagged only.
* ``fanout``       — an additive aggregate of a table's column crosses a join towards the "many" side
  of a declared relationship. Safe correction: ``COUNT(t.key)`` over the one side's own join key
  becomes ``COUNT(DISTINCT t.key)``; every other case is flagged.
* ``dq_gate``      — a recipe data-quality gate failed on a table the step read. Flagged only.
* ``numbers``      — (claims) every number in the text binds to a value of an upstream result.
"""
from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

VERSION = "selfcheck.v1"
CHECKS = ("empty_result", "magnitude", "truncation", "grouping", "fanout", "dq_gate", "numbers")
MAGNITUDE_FACTOR = 10.0
ADDITIVE = (exp.Sum, exp.Avg, exp.Count)
ONE_SIDE = {"many_to_one": ("from", "to"), "one_to_many": ("to", "from")}  # (many side, one side)


@dataclass
class Observation:
    """What a step produced and what the checks may compare it with."""

    kind: str = "query"
    sql: str | None = None
    dialect: str = "postgres"
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int | None = None
    truncated: bool = False
    history: list[float] = field(default_factory=list)  # headline values of the step's earlier versions
    populations: dict[str, dict[str, Any]] = field(default_factory=dict)  # asset -> population_check (P4-C12)
    relationships: list[dict[str, Any]] = field(default_factory=list)  # {from_table, from_column, to_table, to_column, cardinality}
    dq: list[dict[str, Any]] = field(default_factory=list)  # {asset, gate, status, detail}
    column_values: dict[str, list[Any]] = field(default_factory=dict)  # "table.column" | "column" -> profiled values
    text: str | None = None  # claims
    upstream_values: list[float] = field(default_factory=list)  # claims: numbers the text may quote


@dataclass
class CheckResult:
    check: str
    passed: bool
    detail: str
    severity: str = "error"
    evidence: dict[str, Any] = field(default_factory=dict)
    correction: dict[str, Any] | None = None  # {"sql": ..., "reason": ...}: safe to apply and re-run

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "passed": self.passed, "severity": self.severity if not self.passed else "info",
                "detail": self.detail, "evidence": self.evidence}


# ------------------------------------------------------------------------------------ helpers
def _parse(sql: str | None, dialect: str) -> exp.Expression | None:
    if not sql:
        return None
    try:
        return sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - an unparsable statement is judged on its result only
        return None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return float(v) if math.isfinite(float(v)) else None
    try:
        f = float(str(v))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def numeric_columns(columns: list[str], rows: list[list[Any]]) -> list[int]:
    """Columns whose every non-null value is a number (and that have at least one)."""
    out = []
    for i in range(len(columns)):
        vals = [r[i] for r in rows if i < len(r) and r[i] is not None]
        if vals and all(isinstance(v, int | float) and not isinstance(v, bool) for v in vals):
            out.append(i)
    return out


def headline(columns: list[str], rows: list[list[Any]]) -> float | None:
    """The one number a result is about: a scalar, or the total of the last numeric column."""
    nums = numeric_columns(columns, rows)
    if not rows or not nums:
        return None
    if len(rows) == 1:
        return _num(rows[0][nums[0]])
    return float(sum(_num(r[nums[-1]]) or 0.0 for r in rows))


def _aliases(tree: exp.Expression) -> dict[str, str]:
    """alias (or bare name) -> table name, lower-cased."""
    out: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        out[name] = name
        if t.alias:
            out[t.alias.lower()] = name
    return out


def _table_of(col: exp.Column, aliases: Mapping[str, str]) -> str | None:
    if col.table:
        return aliases.get(col.table.lower(), col.table.lower())
    tables = set(aliases.values())
    return next(iter(tables)) if len(tables) == 1 else None


def _short(table: str) -> str:
    return table.rsplit(".", 1)[-1].lower()


# ------------------------------------------------------------------------------------ checks
def empty_result(obs: Observation) -> CheckResult:
    n = obs.row_count if obs.row_count is not None else len(obs.rows)
    if n > 0:
        return CheckResult("empty_result", True, f"{n} rows")
    tree = _parse(obs.sql, obs.dialect)
    fixes: list[dict[str, Any]] = []
    if tree is not None:
        aliases = _aliases(tree)
        for lit in list(tree.find_all(exp.Literal)):
            if not lit.is_string:
                continue
            parent = lit.parent
            if isinstance(parent, exp.In):
                col = parent.this
            elif isinstance(parent, exp.EQ):
                col = parent.left if parent.right is lit else parent.right
            else:
                continue
            if not isinstance(col, exp.Column):
                continue
            table = _table_of(col, aliases)
            known = obs.column_values.get(f"{table}.{col.name.lower()}" if table else "") or \
                obs.column_values.get(col.name.lower()) or []
            value = lit.this
            if value in known:
                continue
            same = sorted({str(k) for k in known if isinstance(k, str) and k.casefold() == value.casefold() and k != value})
            if len(same) == 1:
                fixes.append({"column": col.sql(), "from": value, "to": same[0]})
                lit.replace(exp.Literal.string(same[0]))
    if fixes:
        corrected = tree.sql(dialect=obs.dialect)
        what = ", ".join(f"{f['column']} = '{f['from']}' -> '{f['to']}'" for f in fixes)
        return CheckResult("empty_result", False, f"no rows; a filter value matches the data only ignoring case ({what})",
                           evidence={"fixes": fixes},
                           correction={"sql": corrected, "reason": f"filter values corrected to their profiled spelling: {what}"})
    return CheckResult("empty_result", False, "no rows: check the filters, the period and the table",
                       evidence={"rows": 0})


def magnitude(obs: Observation) -> CheckResult:
    value = headline(obs.columns, obs.rows)
    history = [h for h in obs.history if h is not None and math.isfinite(h)]
    if value is None or not history:
        return CheckResult("magnitude", True, "no earlier result to compare with" if value is not None else "no headline number",
                           evidence={"value": value, "history": len(history)})
    ref = statistics.median(history)
    if ref == 0:
        return CheckResult("magnitude", True, f"value {value:g}; the median of earlier results is 0, so no ratio applies",
                           evidence={"value": value, "median": ref})
    ratio = value / ref
    evidence = {"value": value, "median": ref, "ratio": round(ratio, 6), "history": len(history)}
    if ratio < 0 and abs(value) > abs(ref) * 0.01:
        return CheckResult("magnitude", False, f"value {value:g} has the opposite sign of earlier results (median {ref:g})",
                           evidence=evidence)
    if abs(ratio) > MAGNITUDE_FACTOR or abs(ratio) < 1 / MAGNITUDE_FACTOR:
        unit = ""
        if any(abs(abs(ratio) - k) / k < 0.05 for k in (100.0, 0.01)):
            unit = "; a factor of 100 is typical of a percent vs fraction unit mistake"
        return CheckResult("magnitude", False, f"value {value:g} is {abs(ratio):.3g}x the median of earlier results ({ref:g})"
                           + unit, evidence=evidence)
    return CheckResult("magnitude", True, f"value {value:g} within 10x of earlier results (median {ref:g})", evidence=evidence)


def truncation(obs: Observation) -> CheckResult:
    problems, evidence = [], {}
    if obs.truncated:
        problems.append(f"the result was cut at the row cap ({obs.row_count if obs.row_count is not None else len(obs.rows)} rows)")
        evidence["truncated"] = True
    for asset, check in sorted(obs.populations.items()):
        if check and not check.get("passed", True):
            problems.append(f"{asset}: {check.get('detail') or 'truncated snapshot'}")
            evidence.setdefault("populations", {})[asset] = check
    if problems:
        return CheckResult("truncation", False, "the numbers describe part of the data: " + "; ".join(problems),
                           evidence=evidence)
    return CheckResult("truncation", True, "complete result on a declared population", evidence=evidence)


def grouping(obs: Observation) -> CheckResult:
    problems: list[str] = []
    evidence: dict[str, Any] = {}
    nums = set(numeric_columns(obs.columns, obs.rows))
    keys = [i for i in range(len(obs.columns)) if i not in nums]
    if keys and nums and len(obs.rows) > 1:
        seen: dict[tuple, int] = {}
        for r in obs.rows:
            k = tuple(r[i] if i < len(r) else None for i in keys)
            seen[k] = seen.get(k, 0) + 1
        dup = {k: n for k, n in seen.items() if n > 1}
        if dup:
            first = next(iter(dup))
            problems.append(f"{len(dup)} group(s) appear more than once (e.g. {list(first)} x{dup[first]})")
            evidence["duplicate_groups"] = len(dup)
        folded: dict[tuple, set] = {}
        for k in seen:
            folded.setdefault(tuple(" ".join(str(v).split()).casefold() if isinstance(v, str) else v for v in k), set()).add(k)
        near = [sorted(map(str, v)) for v in folded.values() if len(v) > 1]
        if near:
            problems.append(f"groups differ only in case or spacing: {near[0]}")
            evidence["near_duplicates"] = near[:5]
    tree = _parse(obs.sql, obs.dialect)
    if tree is not None:
        select = tree.find(exp.Select)
        group = select.args.get("group") if select is not None else None
        if select is not None and group is not None:
            shown = {e.alias_or_name.lower() for e in select.expressions} | \
                {c.name.lower() for e in select.expressions for c in e.find_all(exp.Column)}
            hidden = [g.sql() for g in group.expressions if isinstance(g, exp.Column) and g.name.lower() not in shown]
            if hidden:
                problems.append(f"grouped by {', '.join(hidden)} without showing it, so groups repeat")
                evidence["hidden_group_by"] = hidden
    if problems:
        return CheckResult("grouping", False, "; ".join(problems), evidence=evidence)
    return CheckResult("grouping", True, "one row per group", evidence=evidence)


def _cardinality(rels: Iterable[Mapping[str, Any]], a: tuple[str, str], b: tuple[str, str]) -> tuple[str, str] | None:
    """(many side table, one side table) of a declared relationship between column a and column b, or
    ("*", "*") for many_to_many; None when undeclared or one_to_one."""
    for r in rels:
        f = (_short(str(r.get("from_table", ""))), str(r.get("from_column", "")).lower())
        t = (_short(str(r.get("to_table", ""))), str(r.get("to_column", "")).lower())
        if {f, t} != {a, b}:
            continue
        card = str(r.get("cardinality") or "")
        if card == "many_to_many":
            return ("*", "*")
        if card in ONE_SIDE:
            many, one = ONE_SIDE[card]
            return ({"from": f, "to": t}[many][0], {"from": f, "to": t}[one][0])
        return None
    return None


def fanout(obs: Observation) -> CheckResult:
    tree = _parse(obs.sql, obs.dialect)
    if tree is None or not list(tree.find_all(exp.Join)) or not obs.relationships:
        return CheckResult("fanout", True, "no join across a declared one-to-many relationship")
    aliases = _aliases(tree)
    edges: list[tuple[str, str, tuple[str, str], tuple[str, str]]] = []  # (many, one, one-side column, many-side column)
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        for eq in (on.find_all(exp.EQ) if on is not None else []):
            if not (isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column)):
                continue
            a = (_short(_table_of(eq.left, aliases) or ""), eq.left.name.lower())
            b = (_short(_table_of(eq.right, aliases) or ""), eq.right.name.lower())
            card = _cardinality(obs.relationships, a, b)
            if card is None:
                continue
            many, one = card
            one_col = a if a[0] == one else b
            many_col = b if one_col is a else a
            edges.append((many, one, one_col, many_col))
    if not edges:
        return CheckResult("fanout", True, "joins follow declared many-to-one relationships")
    problems, fixes = [], []
    for agg in list(tree.find_all(*ADDITIVE)):
        arg = agg.this
        if isinstance(agg, exp.Count) and isinstance(arg, exp.Distinct):
            continue  # COUNT(DISTINCT ...) is fan-out safe
        cols = list(arg.find_all(exp.Column)) if arg is not None else []
        tables = {_short(_table_of(c, aliases) or "") for c in cols}
        for many, one, one_col, _many_col in edges:
            multiplied = (many == "*" and bool(tables)) or (one in tables) or \
                (isinstance(agg, exp.Count) and (arg is None or isinstance(arg, exp.Star)) and many == "*")
            if not multiplied:
                continue
            if isinstance(agg, exp.Count) and len(cols) == 1 and (_short(_table_of(cols[0], aliases) or ""),
                                                                   cols[0].name.lower()) == one_col:
                fixes.append((agg, cols[0]))
                continue
            problems.append(f"{agg.sql()} adds up rows of {one} after a join that repeats each of them "
                            f"(one {one} to many {many})" if many != "*" else f"{agg.sql()} crosses a many-to-many join")
    if fixes and not problems:
        for agg, col in fixes:
            agg.replace(exp.Count(this=exp.Distinct(expressions=[col.copy()])))
        what = ", ".join(sorted({f"COUNT({c.sql()})" for _, c in fixes}))
        return CheckResult("fanout", False, f"{what} counts join-repeated rows", evidence={"edges": len(edges)},
                           correction={"sql": tree.sql(dialect=obs.dialect),
                                       "reason": f"{what} -> COUNT(DISTINCT ...): the one side's own key counts each row once"})
    if problems or fixes:
        return CheckResult("fanout", False, "; ".join(problems) + (" (pre-aggregate before joining)" if problems else ""),
                           evidence={"edges": len(edges)})
    return CheckResult("fanout", True, "no additive aggregate crosses a one-to-many join", evidence={"edges": len(edges)})


def dq_gate(obs: Observation) -> CheckResult:
    failed = [d for d in obs.dq if str(d.get("status")) in ("failed", "blocked")]
    if failed:
        return CheckResult("dq_gate", False, "data-quality gates failed on: " + "; ".join(
            f"{d.get('asset')} {d.get('gate') or ''} {d.get('detail') or ''}".strip() for d in failed[:5]),
            evidence={"failed": failed[:20]})
    return CheckResult("dq_gate", True, "no failed data-quality gate on the tables read", evidence={"gates": len(obs.dq)})


_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?%?")


def numbers(obs: Observation) -> CheckResult:
    text = obs.text or ""
    found = [m.group(0) for m in _NUMBER.finditer(text)]
    if not found:
        return CheckResult("numbers", True, "no number in the text")
    values = [v for v in obs.upstream_values if v is not None]
    unbound = []
    for token in found:
        raw = float(token.rstrip("%").replace(",", ""))
        decimals = len(token.rstrip("%").split(".", 1)[1]) if "." in token else 0
        tol = 0.5 * 10 ** (-decimals)  # the rounding the text shows, in its own unit
        scales = (1.0, 100.0) if token.endswith("%") else (1.0,)  # "31.2%" quotes 0.312 or 31.2
        if not any(abs(raw - v * k) <= tol for v in values for k in scales):
            unbound.append(token)
    if unbound:
        return CheckResult("numbers", False, f"{', '.join(unbound[:5])} bind to no upstream result",
                           evidence={"unbound": unbound, "numbers": found})
    return CheckResult("numbers", True, f"{len(found)} number(s) bound to upstream results", evidence={"numbers": found})


RESULT_CHECKS = (empty_result, magnitude, truncation, grouping, fanout, dq_gate)


def run(obs: Observation) -> list[CheckResult]:
    """Every check that applies to the step's kind, in a fixed order."""
    if obs.kind == "claim":
        return [numbers(obs)]
    if obs.kind in ("plan", "recipe", "train", "chart"):
        return []
    return [c(obs) for c in RESULT_CHECKS]


def safe_correction(results: Iterable[CheckResult]) -> CheckResult | None:
    """The first failed check with a safe correction (checks are ordered; one correction per round)."""
    return next((r for r in results if not r.passed and r.correction), None)


def passed(results: Iterable[CheckResult]) -> bool:
    return all(r.passed or r.severity != "error" for r in results)


__all__ = ["CHECKS", "VERSION", "CheckResult", "Observation", "dq_gate", "empty_result", "fanout", "grouping", "headline",
           "magnitude", "numbers", "passed", "run", "safe_correction", "truncation"]
