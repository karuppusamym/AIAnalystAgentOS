"""Row-level security (P7-02, ADR-0019): render a policy row filter for one caller and apply it to a
statement.

A `RowFilter` predicate is SQL over the asset's own columns with `{{user.<attr>}}` placeholders. The
placeholders are never substituted as text: the predicate is parsed with sentinel identifiers and each
sentinel becomes a literal node (a list inside `IN` expands to one literal per value), so an attribute
value cannot change the statement's shape. A missing attribute raises `MissingAttribute`; the scope
resolver withholds the asset (fail closed).

`wrap_tables` is the one place a filter is applied: every reference to a filtered asset becomes
`(SELECT <permitted columns> FROM asset WHERE <predicates>) AS <alias>`. The semantic compiler calls it
while building a statement and the gateway validator calls it on every statement; a reference that is
already wrapped in exactly that form is left alone, so compiled SQL passes through unchanged.
"""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

_PLACEHOLDER = re.compile(r"\{\{\s*user\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SENTINEL = "__analystos_attr_{}__"
_FORBIDDEN = (exp.Query, exp.Subquery, exp.AggFunc, exp.Window, exp.Placeholder, exp.Parameter)


class MissingAttribute(ValueError):
    """The caller has no value for an attribute the filter needs."""

    def __init__(self, attribute: str, filter_id: str = ""):
        super().__init__(f"row filter {filter_id or '?'} needs the user attribute '{attribute}', which is not set")
        self.attribute = attribute
        self.filter_id = filter_id


def _parse(predicate: str, dialect: str | None) -> tuple[exp.Expression, list[str]]:
    names: list[str] = []

    def sub(m: re.Match[str]) -> str:
        names.append(m.group(1))
        return _SENTINEL.format(len(names) - 1)

    text = _PLACEHOLDER.sub(sub, predicate)
    if "{{" in text or "}}" in text:
        raise ValueError("only {{user.<attribute>}} placeholders are allowed")
    statements = sqlglot.parse(text, read=dialect or None)
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("a row filter is one boolean expression")
    tree = statements[0]
    if isinstance(tree, exp.Select) or any(isinstance(n, _FORBIDDEN) for n in tree.walk()):
        raise ValueError("a row filter may not contain queries, aggregates, windows or bind parameters")
    for col in tree.find_all(exp.Column):
        if col.table:
            raise ValueError(f"row filter columns are the asset's own, unqualified ({col.sql()})")
    return tree, names


def predicate_problem(predicate: str) -> str | None:
    try:
        _parse(predicate, None)
    except (ValueError, sqlglot.errors.SqlglotError) as exc:
        return str(exc).splitlines()[0]
    return None


def _literal(value: Any) -> exp.Expression:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int | float):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _attribute(user: dict[str, Any], name: str, filter_id: str) -> Any:
    value = user.get(name)
    if value is None or value == "":
        raise MissingAttribute(name, filter_id)
    return value


def render(predicate: str, user: dict[str, Any], dialect: str, *, filter_id: str = "") -> tuple[str, list[str]]:
    """(predicate SQL in `dialect` with the caller's values as literals, the columns it reads).
    `user` holds the caller's attributes plus id, email and role."""
    tree, names = _parse(predicate, dialect)
    values = [_attribute(user, n, filter_id) for n in names]
    sentinels = {_SENTINEL.format(i): v for i, v in enumerate(values)}

    def swap(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.In) and isinstance(node.args.get("field"), exp.Column) and node.args["field"].name in sentinels:
            value = sentinels[node.args["field"].name]
            items = value if isinstance(value, list) else [value]
            if not items:  # nobody is in an empty set; NOT IN () is handled by the enclosing Not
                return exp.false()
            return exp.In(this=node.this, expressions=[_literal(v) for v in items])
        if isinstance(node, exp.Column) and node.name in sentinels:
            value = sentinels[node.name]
            if isinstance(value, list):
                raise ValueError(f"row filter {filter_id}: a list attribute can only be used with IN")
            return _literal(value)
        return node

    rendered = tree.transform(swap)
    columns = sorted({c.name for c in rendered.find_all(exp.Column)})
    return rendered.sql(dialect=dialect), columns


def _matches(pattern: str, fq: str) -> bool:
    if pattern.startswith("*."):
        return fq.rsplit(".", 1)[-1].lower() == pattern[2:].lower()
    return pattern.lower() == fq.lower()


def applies(patterns: list[str], asset: str) -> bool:
    return any(_matches(p, asset) for p in patterns)


# ------------------------------------------------------------------------------------ application
def _ident(name: str, fold: str) -> exp.Identifier:
    return exp.to_identifier(name.upper() if fold == "upper" else name, quoted=True)


def _wrapper(table: exp.Table, columns: list[str], predicates: list[str], dialect: str, fold: str) -> exp.Select:
    inner = exp.Table(this=table.this.copy(), db=table.args["db"].copy() if table.args.get("db") else None)
    select = exp.select(*[exp.column(_ident(c, fold)) for c in columns]).from_(inner)
    for p in predicates:
        select = select.where(sqlglot.parse_one(p, read=dialect))
    return select


def _canonical(select: exp.Select, dialect: str) -> str:
    """The wrapper's shape without what qualification adds (table aliases, column qualifiers,
    `c AS c`, quoting), so a validated statement is recognised as already wrapped."""

    def unalias(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Alias) and isinstance(node.this, exp.Column) and node.this.name.lower() == node.alias.lower():
            return node.this.copy()
        return node

    def strip(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.args.get("table") is not None:
            node = node.copy()
            node.set("table", None)
        if isinstance(node, exp.Table) and node.args.get("alias") is not None:
            node = node.copy()
            node.set("alias", None)
        return node

    if select.args.get("joins") or select.args.get("group") or select.args.get("having"):
        return ""  # never the wrapper's shape
    bare = select.copy().transform(unalias).transform(strip)
    for ident in bare.find_all(exp.Identifier):
        ident.set("this", ident.name.lower())
        ident.set("quoted", True)
    return bare.sql(dialect=dialect)


def permitted_columns(scope: Any, asset: str) -> list[str]:
    denied = {d.lower() for d in scope.denied_columns if not d.startswith("*.")}
    denied_any = {d[2:].lower() for d in scope.denied_columns if d.startswith("*.")}
    return [c for c in scope.columns.get(asset, [])
            if f"{asset}.{c}".lower() not in denied and c.lower() not in denied_any]


def wrap_tables(tables: list[tuple[exp.Table, str]], scope: Any, dialect: str, fold: str = "lower") -> int:
    """Wrap each (table node, asset) that has row filters in the caller's scope; returns how many were
    wrapped. A reference that already sits in the exact wrapper is left alone (idempotent)."""
    wrapped = 0
    for table, asset in tables:
        predicates = scope.row_filters.get(asset)
        if not predicates:
            continue
        expected = _wrapper(table, permitted_columns(scope, asset), predicates, dialect, fold)
        holder = table.parent.parent if isinstance(table.parent, exp.From) else None
        if isinstance(holder, exp.Select) and isinstance(holder.parent, exp.Subquery) \
                and _canonical(holder, dialect) == _canonical(expected, dialect):
            continue
        alias = table.args["alias"].this.copy() if table.alias else table.this.copy()
        table.replace(exp.Subquery(this=expected, alias=exp.TableAlias(this=alias)))
        wrapped += 1
    return wrapped
