"""Deterministic check of the text literals in model-written SQL against the values a column is known to hold.

A model that never sees a column's values (the workspace keeps data samples away from models) writes
`returned = 'yes'` where the data says 'Yes': the query runs, and every rate is 0. The profile knows the complete
value set of a low-cardinality column, so code decides: a literal that matches a value only in letter case is
rewritten to that value (nothing is sent to the model), and a literal that matches no value at all is refused, so the
repair loop asks for another statement. Only `=`, `<>` and `IN` against a plain column are checked; a column whose
value set is not complete is never judged."""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from analystos.core.errors import SQLRejected


@dataclass
class LiteralCheck:
    sql: str
    rewrites: list[str] = field(default_factory=list)  # "orders.returned: 'yes' read as 'Yes'"


def check_literals(sql: str, dialect: str, known: dict[str, set[str]]) -> LiteralCheck:
    """`known` maps a column name (lower case) to the complete set of values it holds (the union over the tables
    that have a column of that name). Returns the statement (rewritten only when a letter case was fixed)."""
    if not known:
        return LiteralCheck(sql)
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return LiteralCheck(sql)  # the gateway's validator reports a statement it cannot read
    rewrites: list[str] = []
    for column, literal in _comparisons(tree):
        values = known.get(column.name.lower())
        text = literal.this
        if values is None or text in values:
            continue
        folded = [v for v in values if v.lower() == text.lower()]
        if len(folded) == 1:
            literal.replace(exp.Literal.string(folded[0]))
            rewrites.append(f"{column.name}: '{text}' read as '{folded[0]}'")
            continue
        raise SQLRejected(f"'{text}' is not a value of column {column.name} (it holds {len(values)} known values): compare "
                          f"with a value the column holds, or group by the column instead of filtering it",
                          details={"column": column.name, "literal": text})
    return LiteralCheck(tree.sql(dialect=dialect) if rewrites else sql, rewrites)


def _comparisons(tree: exp.Expression) -> list[tuple[exp.Column, exp.Literal]]:
    out: list[tuple[exp.Column, exp.Literal]] = []
    for node in tree.find_all(exp.EQ, exp.NEQ):
        a, b = node.this, node.expression
        if isinstance(a, exp.Literal) and isinstance(b, exp.Column):
            a, b = b, a
        if isinstance(a, exp.Column) and isinstance(b, exp.Literal) and b.is_string:
            out.append((a, b))
    for node in tree.find_all(exp.In):
        if isinstance(node.this, exp.Column):
            out += [(node.this, e) for e in node.expressions if isinstance(e, exp.Literal) and e.is_string]
    return out
