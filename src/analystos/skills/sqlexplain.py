"""Explain a SQL statement without a model: structure from the parse tree, English from templates.

Answers "what does this query do?" for the console, the Ask box and agent traces at zero tokens.
The output only restates what the SQL contains; it never guesses intent.
"""
from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from analystos.core.errors import InvalidInput

_AGG = {"count": "count", "sum": "total", "avg": "average", "min": "minimum", "max": "maximum", "stddev": "standard deviation",
        "variance": "variance", "median": "median", "percentile_cont": "percentile", "approx_distinct": "approximate distinct count"}


def _name(node: exp.Expression) -> str:
    return node.sql(dialect="postgres").replace('"', "")


def _table(t: exp.Table) -> str:
    return ".".join(p for p in (t.catalog, t.db, t.name) if p)


def _agg_phrase(node: exp.AggFunc) -> str:
    key = node.key.lower()
    word = _AGG.get(key, key)
    arg = node.this
    if key == "count":
        if isinstance(node.this, exp.Distinct):
            return "number of distinct " + ", ".join(_name(e) for e in node.this.expressions)
        return "row count" if arg is None or isinstance(arg, exp.Star) else f"count of {_name(arg)}"
    return f"{word} of {_name(arg)}" if arg is not None else word


def explain_sql(sql: str, dialect: str = "postgres") -> dict[str, Any]:
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise InvalidInput(f"cannot parse SQL: {str(exc).splitlines()[0][:200]}") from exc
    if len(statements) != 1:
        raise InvalidInput("explain one statement at a time")
    root = statements[0]
    select = root if isinstance(root, exp.Select) else root.find(exp.Select)
    if select is None:
        return {"statement": root.key.upper(), "read_only": False, "summary": f"A {root.key.upper()} statement (not a query)."}
    ctes = [c.alias for c in root.find_all(exp.CTE)]
    tables = sorted({_table(t) for t in root.find_all(exp.Table) if t.name not in ctes})
    joins = [{"kind": (j.args.get("side") or j.args.get("kind") or "inner").lower() if isinstance(j.args.get("side") or j.args.get("kind"), str)
              else "inner", "table": _table(j.this) if isinstance(j.this, exp.Table) else _name(j.this),
              "on": _name(j.args["on"]) if j.args.get("on") else None} for j in select.args.get("joins") or []]
    aggregations = [_agg_phrase(a) for e in select.expressions for a in e.find_all(exp.AggFunc)]
    plain = [e.alias_or_name for e in select.expressions if not isinstance(e, exp.Star) and e.find(exp.AggFunc) is None]
    group = [_name(g) for g in (select.args.get("group").expressions if select.args.get("group") else [])]
    where = select.args.get("where")
    having = select.args.get("having")
    order = [(_name(o.this) + (" descending" if o.args.get("desc") else "")) for o in
             (select.args.get("order").expressions if select.args.get("order") else [])]
    limit = select.args.get("limit")
    limit_n = _name(limit.expression) if limit is not None and limit.expression is not None else None
    windows = [_name(w) for w in select.find_all(exp.Window)]
    outputs = [e.alias_or_name for e in select.expressions]

    parts = []
    if any(isinstance(e, exp.Star) for e in select.expressions):
        what = "all columns"
    else:
        items = plain[:4] + aggregations[:4]
        what = ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1] if items else "expressions"
    parts.append(f"Returns {what}")
    if tables:
        parts[-1] += " from " + ", ".join(tables[:4]) + (f" (+{len(tables) - 4} more)" if len(tables) > 4 else "")
    if joins:
        parts.append("joining " + "; ".join(f"{j['table']} ({j['kind']}{' on ' + j['on'] if j['on'] else ''})" for j in joins[:3]))
    if where is not None:
        parts.append(f"keeping rows where {_name(where.this)}")
    if group:
        parts.append("grouped by " + ", ".join(group))
    if having is not None:
        parts.append(f"keeping groups where {_name(having.this)}")
    if order:
        parts.append("ordered by " + ", ".join(order))
    if limit_n:
        parts.append(f"limited to {limit_n} rows")
    if ctes:
        parts.append(f"using {len(ctes)} named subquer{'y' if len(ctes) == 1 else 'ies'} ({', '.join(ctes)})")
    return {"statement": "SELECT", "read_only": True, "tables": tables, "ctes": ctes, "outputs": outputs, "joins": joins,
            "filter": _name(where.this) if where is not None else None, "group_by": group, "aggregations": aggregations,
            "having": _name(having.this) if having is not None else None, "order_by": order, "limit": limit_n,
            "window_functions": len(windows), "distinct": bool(select.args.get("distinct")),
            "summary": ", ".join(parts) + "."}
