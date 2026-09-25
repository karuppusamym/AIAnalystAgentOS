"""Value-free query-history mining (P4-K06, spec v3 §6.3 "structure only, never values").

Input: SQL statements the platform already audited (`query_execution`). Output: which tables are
read together and on which columns they join, which columns are selected, filtered, grouped and
ordered, and with which operator class a column is filtered. Literals are never read into the
result: a predicate contributes only `(table, column, operator class)`, so `email = 'a@b.c'` and
`email = 'x@y.z'` are the same pattern and neither value is kept. Pure; no database.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

UNRESOLVED = "?"
_OPERATORS: tuple[tuple[type[exp.Expression], str], ...] = (
    (exp.EQ, "="), (exp.NEQ, "<>"), (exp.GT, "range"), (exp.GTE, "range"), (exp.LT, "range"), (exp.LTE, "range"),
    (exp.Between, "range"), (exp.In, "in"), (exp.Like, "like"), (exp.ILike, "like"), (exp.Is, "is null"),
)


@dataclass
class QueryPatterns:
    statements: int = 0
    parsed: int = 0
    unparsed: int = 0
    tables: Counter = field(default_factory=Counter)  # table -> statements reading it
    joins: Counter = field(default_factory=Counter)  # (left table, left col, right table, right col) sorted pair
    filters: Counter = field(default_factory=Counter)  # (table, column, operator class)
    columns: Counter = field(default_factory=Counter)  # (table, column, usage) usage: select|filter|group|order|join
    groupings: Counter = field(default_factory=Counter)  # tuple of (table.column) grouped together

    def as_dict(self, top: int = 25) -> dict[str, Any]:
        return {"statements": self.statements, "parsed": self.parsed, "unparsed": self.unparsed,
                "tables": [{"table": t, "count": n} for t, n in _top(self.tables, top)],
                "joins": [{"left": f"{a}.{b}", "right": f"{c}.{d}", "count": n} for (a, b, c, d), n in _top(self.joins, top)],
                "filters": [{"column": f"{t}.{c}", "operator": op, "count": n} for (t, c, op), n in _top(self.filters, top)],
                "columns": [{"column": f"{t}.{c}", "usage": u, "count": n} for (t, c, u), n in _top(self.columns, top)],
                "groupings": [{"columns": list(g), "count": n} for g, n in _top(self.groupings, top)]}


def _top(counter: Counter, n: int) -> list[tuple[Any, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))[:n]


def _table_name(t: exp.Table) -> str:
    return ".".join(p for p in (t.catalog, t.db, t.name) if p).lower()


def _scope(select: exp.Select, ctes: frozenset[str] = frozenset()) -> dict[str, str]:
    """{alias or name: table} for the tables directly in this SELECT's FROM and JOINs (not subqueries)."""
    out: dict[str, str] = {}
    sources = []
    frm = select.args.get("from_") or select.args.get("from")
    if frm is not None:
        sources.append(frm.this)
    sources.extend(j.this for j in select.args.get("joins") or [])
    for src in sources:
        if isinstance(src, exp.Table) and src.name and not (not src.db and src.name.lower() in ctes):
            name = _table_name(src)
            out[src.alias_or_name.lower()] = name
            out.setdefault(src.name.lower(), name)
    return out


def _resolve(col: exp.Column, scope: dict[str, str]) -> tuple[str, str] | None:
    """(table, column) for a column reference, or None when it names something outside this scope
    (a CTE or subquery alias). An unqualified column is attributed only when one table is in scope."""
    name = col.name.lower()
    if not name or name == "*":
        return None
    qual = col.table.lower()
    if qual:
        return (scope[qual], name) if qual in scope else None
    tables = set(scope.values())
    return (tables.pop(), name) if len(tables) == 1 else (UNRESOLVED, name)


def _own_columns(node: exp.Expression | None, select: exp.Select) -> list[exp.Column]:
    """Columns under `node` that belong to `select`, not to a nested SELECT."""
    if node is None:
        return []
    return [c for c in node.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]


def _operator(pred: exp.Expression) -> str | None:
    for cls, name in _OPERATORS:
        if isinstance(pred, cls):
            return name
    return None


def _mine_select(select: exp.Select, p: QueryPatterns, ctes: frozenset[str] = frozenset()) -> None:
    scope = _scope(select, ctes)
    if not scope:
        return
    for sel in select.expressions:
        for c in _own_columns(sel, select):
            if (r := _resolve(c, scope)) is not None:
                p.columns[(r[0], r[1], "select")] += 1
    predicates = [select.args.get("where"), *[j.args.get("on") for j in select.args.get("joins") or []]]
    for root in predicates:  # join paths: column = column across two tables, in ON or (implicit joins) WHERE
        for eq in (root.find_all(exp.EQ) if root is not None else []):
            left, right = eq.this, eq.expression
            if eq.find_ancestor(exp.Select) is select and isinstance(left, exp.Column) and isinstance(right, exp.Column):
                a, b = _resolve(left, scope), _resolve(right, scope)
                if a and b and UNRESOLVED not in (a[0], b[0]) and a[0] != b[0]:
                    p.joins[tuple(x for pair in sorted([a, b]) for x in pair)] += 1
                    p.columns[(a[0], a[1], "join")] += 1
                    p.columns[(b[0], b[1], "join")] += 1
    for root in predicates:
        if root is None:
            continue
        for pred in root.find_all(*(cls for cls, _ in _OPERATORS)):
            if pred.find_ancestor(exp.Select) is not select:
                continue
            op = _operator(pred)
            target = pred.this
            is_join = isinstance(pred, exp.EQ) and isinstance(pred.expression, exp.Column)
            if isinstance(target, exp.Column) and not is_join and (r := _resolve(target, scope)) is not None:
                p.filters[(r[0], r[1], op)] += 1
                p.columns[(r[0], r[1], "filter")] += 1
    group = select.args.get("group")
    if group is not None:
        cols = []
        for c in _own_columns(group, select):
            if (r := _resolve(c, scope)) is not None:
                p.columns[(r[0], r[1], "group")] += 1
                cols.append(f"{r[0]}.{r[1]}")
        if cols:
            p.groupings[tuple(sorted(set(cols)))] += 1
    for c in _own_columns(select.args.get("order"), select):
        if (r := _resolve(c, scope)) is not None:
            p.columns[(r[0], r[1], "order")] += 1


def mine(statements: Iterable[str], *, dialect: str = "postgres") -> QueryPatterns:
    p = QueryPatterns()
    for sql in statements:
        p.statements += 1
        try:
            trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
            p.unparsed += 1
            continue
        if not trees:
            p.unparsed += 1
            continue
        p.parsed += 1
        seen_tables: set[str] = set()
        for tree in trees:
            ctes = frozenset(c.alias_or_name.lower() for c in tree.find_all(exp.CTE))
            for t in tree.find_all(exp.Table):
                name = _table_name(t)
                if name and name not in ctes:
                    seen_tables.add(name)
            for select in tree.find_all(exp.Select):
                _mine_select(select, p, ctes)
        for name in seen_tables:
            p.tables[name] += 1
    return p
