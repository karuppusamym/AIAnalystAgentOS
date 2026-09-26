"""Recipe IR -> SQL, per dialect (ADR-0023 decision 2). Every statement is built as a sqlglot tree
and only rendered at the end, so a column name or a literal can never become SQL text by
concatenation (DataPilot's `pipeline_codegen/` f-strings are deliberately not ported, ADR-0018 §4).

One output compiles to one read-only statement: a CTE per node of its ancestry (`n_<node id>`),
then a final SELECT of the declared output columns. Sources are read through their declared
schema with an explicit CAST to the declared type, so the SQL engine and the DuckDB snapshot engine
see the same types. The same builder produces the gate flags (P6-05) and the join pre-flight
(distinct keys, overlap, row multiplication), so what is checked is exactly what runs.

The `sql` target runs through `QueryGateway.execute` (the gateway validates it again, fail closed);
the `duckdb` target runs over an immutable snapshot (`recipes/snapshot.py`); the dbt target renders
the postgres form into a dbt model (`recipes/dbt.py`).
"""
from __future__ import annotations

from typing import Any

from sqlglot import exp

from analystos.contracts.recipe import (
    AggregateNode,
    CastNode,
    DedupeNode,
    DeriveNode,
    FilterNode,
    Gate,
    JoinNode,
    OrderKey,
    OutputNode,
    RenameNode,
    SelectNode,
    SourceNode,
    UnionNode,
    ValidatedRecipe,
    WindowNode,
    family,
    sql_type,
)
from analystos.core.errors import InvalidInput

COMPILER_DIALECTS = ("postgres", "duckdb", "tsql")
FLAG_PREFIX = "aos_gate_"
ROW_NUMBER = "aos_rn"


def ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def col(name: str, table: str | None = None) -> exp.Column:
    return exp.Column(this=ident(name), table=ident(table) if table else None)


def cte_name(node_id: str) -> str:
    return f"n_{node_id}"


def _table(name: str, alias: str | None = None) -> exp.Table:
    t = exp.Table(this=ident(name))
    if alias:
        t.set("alias", exp.TableAlias(this=ident(alias)))
    return t


def _asset_table(asset: str) -> exp.Table:
    schema, table = asset.split(".", 1)
    return exp.Table(this=ident(table), db=ident(schema))


def _cast(e: exp.Expression, ctype: str) -> exp.Cast:
    return exp.Cast(this=e, to=sql_type(ctype))


def _quote_columns(tree: exp.Expression) -> exp.Expression:
    tree = tree.copy()
    for c in list(tree.find_all(exp.Column)):
        c.replace(col(c.name))
    return tree


def _ordered(keys: list[OrderKey], table: str | None = None) -> list[exp.Ordered]:
    return [exp.Ordered(this=col(k.column, table), desc=k.desc, nulls_first=False) for k in keys]


def _count_star() -> exp.Count:
    return exp.Count(this=exp.Star())


def _and(conds: list[exp.Expression]) -> exp.Expression:
    out = conds[0]
    for c in conds[1:]:
        out = exp.And(this=out, expression=c)
    return out


def _literal(value: Any) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


class Compiler:
    def __init__(self, validated: ValidatedRecipe, dialect: str = "postgres") -> None:
        if dialect not in COMPILER_DIALECTS:
            raise InvalidInput(f"no recipe compiler for dialect {dialect} (supported: {', '.join(COMPILER_DIALECTS)})")
        self.v = validated
        self.dialect = dialect

    # ------------------------------------------------------------------ one node
    def _names(self, node_id: str) -> list[str]:
        return [c.name for c in self.v.schemas[node_id]]

    def node_select(self, node: Any) -> exp.Query:
        v = self.v
        if isinstance(node, SourceNode):
            # Qualified by the table's own name, so SQL lineage of the emitted model resolves without a catalog.
            alias = node.asset.split(".", 1)[1]
            table = _asset_table(node.asset)
            table.set("alias", exp.TableAlias(this=ident(alias)))
            return exp.select(*[exp.alias_(_cast(col(c.name, alias), c.type), ident(c.name))
                                for c in node.output_schema or []]).from_(table)
        if isinstance(node, OutputNode):
            return exp.select(*[col(c.name) for c in node.output_schema or []]).from_(_table(cte_name(node.input)))
        if isinstance(node, SelectNode):
            return exp.select(*[col(c) for c in node.columns]).from_(_table(cte_name(node.input)))
        if isinstance(node, FilterNode):
            pred = _quote_columns(v.exprs[(node.id, "predicate")])
            return exp.select(*[col(c) for c in self._names(node.input)]).from_(_table(cte_name(node.input))).where(pred)
        if isinstance(node, CastNode):
            cols = v.columns(node.id)
            return exp.select(*[exp.alias_(_cast(col(n), cols[n]), ident(n)) if n in node.casts else col(n)
                                for n in self._names(node.input)]).from_(_table(cte_name(node.input)))
        if isinstance(node, DeriveNode):
            items: list[exp.Expression] = [col(c) for c in self._names(node.input)]
            for d in node.columns:
                # The validator proved the expression is of the declared type in the IR; the CAST pins it on
                # engines whose functions differ (Postgres DATE_TRUNC on a date returns a timestamp).
                tree = _quote_columns(v.exprs[(node.id, d.name)])
                if not isinstance(tree, exp.Cast):
                    tree = _cast(tree, d.type)
                items.append(exp.alias_(tree, ident(d.name)))
            return exp.select(*items).from_(_table(cte_name(node.input)))
        if isinstance(node, RenameNode):
            return exp.select(*[exp.alias_(col(n), ident(node.mapping.get(n, n))) if n in node.mapping else col(n)
                                for n in self._names(node.input)]).from_(_table(cte_name(node.input)))
        if isinstance(node, DedupeNode):
            names = self._names(node.input)
            ordered = {k.column for k in node.order}
            # Every other column breaks ties, so the kept row is the same on every engine.
            order = [*node.order, *[OrderKey(column=c) for c in names if c not in ordered]]
            window = exp.Window(this=exp.RowNumber(), partition_by=[col(k) for k in node.keys],
                                order=exp.Order(expressions=_ordered(order)))
            inner = exp.select(*[col(c) for c in names], exp.alias_(window, ident(ROW_NUMBER))) \
                .from_(_table(cte_name(node.input)))
            return exp.select(*[col(c) for c in names]).from_(exp.alias_(exp.Subquery(this=inner), "d", table=True)) \
                .where(exp.EQ(this=col(ROW_NUMBER), expression=exp.Literal.number(1)))
        if isinstance(node, JoinNode):
            right_keys = {k.right for k in node.on}
            left_cols = self._names(node.left)
            items = [col(c, "l") for c in left_cols]
            items += [col(c, "r") for c in self._names(node.right) if c not in right_keys]
            on = _and([exp.EQ(this=col(k.left, "l"), expression=col(k.right, "r")) for k in node.on])
            return exp.select(*items).from_(_table(cte_name(node.left), "l")).join(
                _table(cte_name(node.right), "r"), on=on, join_type="left" if node.how == "left" else "inner")
        if isinstance(node, AggregateNode):
            items = [col(k) for k in node.keys]
            for m in node.measures:
                if m.func == "count":
                    agg: exp.Expression = exp.Count(this=col(m.column)) if m.column else _count_star()
                elif m.func == "count_distinct":
                    agg = exp.Count(this=exp.Distinct(expressions=[col(m.column or "")]))
                else:
                    agg = {"sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}[m.func](this=col(m.column or ""))
                items.append(exp.alias_(_cast(agg, m.type), ident(m.name)))
            q = exp.select(*items).from_(_table(cte_name(node.input)))
            return q.group_by(*[col(k) for k in node.keys]) if node.keys else q
        if isinstance(node, WindowNode):
            items = [col(c) for c in self._names(node.input)]
            for w in node.columns:
                if w.func in ("row_number", "rank", "dense_rank"):
                    fn: exp.Expression = {"row_number": exp.RowNumber, "rank": exp.Rank,
                                          "dense_rank": exp.DenseRank}[w.func]()
                elif w.func in ("lag", "lead"):
                    fn = (exp.Lag if w.func == "lag" else exp.Lead)(this=col(w.column or ""),
                                                                    offset=exp.Literal.number(w.offset))
                elif w.func == "count":
                    fn = exp.Count(this=col(w.column)) if w.column else _count_star()
                else:
                    fn = {"sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}[w.func](this=col(w.column or ""))
                window = exp.Window(this=fn, partition_by=[col(p) for p in w.partition_by] or None,
                                    order=exp.Order(expressions=_ordered(w.order_by)) if w.order_by else None)
                items.append(exp.alias_(_cast(window, w.type), ident(w.name)))
            return exp.select(*items).from_(_table(cte_name(node.input)))
        if isinstance(node, UnionNode):
            names = self._names(node.union_inputs[0])
            parts = [exp.select(*[col(c) for c in names]).from_(_table(cte_name(i))) for i in node.union_inputs]
            out: exp.Query = parts[0]
            for p in parts[1:]:
                out = exp.Union(this=out, expression=p, distinct=node.distinct)
            return out
        raise InvalidInput(f"no compiler for node {node.id}")

    # ------------------------------------------------------------------ statements
    def _with(self, node_ids: list[str], body: exp.Select) -> exp.Select:
        for nid in node_ids:
            body = body.with_(ident(cte_name(nid)), as_=self.node_select(self.v.nodes[nid]))
        return body

    def output_query(self, output_id: str, *, gates: list[Gate] | None = None) -> exp.Select:
        """The statement of one output; `gates` adds one 0/1 flag column per row gate (`aos_gate_<i>`)."""
        node = self.v.nodes.get(output_id)
        if not isinstance(node, OutputNode):
            raise InvalidInput(f"{output_id} is not an output of the recipe")
        names = [c.name for c in node.output_schema or []]
        items: list[exp.Expression] = [col(c) for c in names]
        for i, g in enumerate(gates or []):
            items.append(exp.alias_(gate_flag(g, self.v.columns(output_id)), ident(f"{FLAG_PREFIX}{i}")))
        body = exp.select(*items).from_(_table(cte_name(output_id)))
        return self._with(self.v.ancestors(output_id), body)

    def preflight_query(self, join_id: str) -> exp.Select:
        """Join pre-flight: rows, non-null key rows and distinct keys per side, matched left rows,
        distinct matched keys and the inner-join row count, in one read."""
        node = self.v.nodes.get(join_id)
        if not isinstance(node, JoinNode):
            raise InvalidInput(f"{join_id} is not a join")
        lk, rk = [k.left for k in node.on], [k.right for k in node.on]
        left, right = cte_name(node.left), cte_name(node.right)

        def scalar(q: exp.Select) -> exp.Subquery:
            return exp.Subquery(this=q)

        def notnull(keys: list[str], table: str | None = None) -> exp.Expression:
            return _and([exp.Not(this=exp.Is(this=col(k, table), expression=exp.Null())) for k in keys])

        def distinct_count(keys: list[str], src: exp.Table, *, where: exp.Expression | None = None,
                           join: tuple[exp.Table, exp.Expression] | None = None, table: str | None = None) -> exp.Subquery:
            inner = exp.select(*[col(k, table) for k in keys]).distinct().from_(src)
            if join is not None:
                inner = inner.join(join[0], on=join[1])
            if where is not None:
                inner = inner.where(where)
            return scalar(exp.select(_count_star()).from_(exp.alias_(exp.Subquery(this=inner), "x", table=True)))

        on = _and([exp.EQ(this=col(a, "l"), expression=col(b, "r")) for a, b in zip(lk, rk, strict=True)])
        exists = exp.Exists(this=exp.select(exp.Literal.number(1)).from_(_table(right, "r")).where(on))
        items = {
            "left_rows": scalar(exp.select(_count_star()).from_(_table(left))),
            "left_key_rows": scalar(exp.select(_count_star()).from_(_table(left)).where(notnull(lk))),
            "left_keys": distinct_count(lk, _table(left), where=notnull(lk)),
            "right_rows": scalar(exp.select(_count_star()).from_(_table(right))),
            "right_key_rows": scalar(exp.select(_count_star()).from_(_table(right)).where(notnull(rk))),
            "right_keys": distinct_count(rk, _table(right), where=notnull(rk)),
            "matched_left_rows": scalar(exp.select(_count_star()).from_(_table(left, "l")).where(exists)),
            "matched_keys": distinct_count(lk, _table(left, "l"), join=(_table(right, "r"), on), table="l"),
            "joined_rows": scalar(exp.select(_count_star()).from_(_table(left, "l")).join(_table(right, "r"), on=on)),
        }
        body = exp.select(*[exp.alias_(e, ident(name)) for name, e in items.items()])
        ids = [n.id for n in self.v.recipe.nodes if n.id in set(self.v.ancestors(node.left)) | set(self.v.ancestors(node.right))]
        return self._with(ids, body)

    def sql(self, tree: exp.Expression, *, pretty: bool = False) -> str:
        return tree.sql(dialect=self.dialect, pretty=pretty)


def gate_flag(gate: Gate, cols: dict[str, str]) -> exp.Expression:
    """1 when a row fails a row gate, else 0 (an integer, so every dialect can sum it)."""
    one, zero = exp.Literal.number(1), exp.Literal.number(0)

    def case(cond: exp.Expression) -> exp.Case:
        return exp.Case(ifs=[exp.If(this=cond, true=one)], default=zero)

    def is_null(name: str) -> exp.Expression:
        return exp.Is(this=col(name), expression=exp.Null())

    def not_null(name: str) -> exp.Expression:
        return exp.Not(this=is_null(name))

    if gate.type == "not_null":
        return case(is_null(gate.column or ""))
    if gate.type == "unique":
        keys = gate.targets()
        dup = exp.GT(this=exp.Window(this=_count_star(), partition_by=[col(k) for k in keys]),
                     expression=exp.Literal.number(1))
        return case(_and([*[not_null(k) for k in keys], dup]))
    if gate.type == "accepted_values":
        name = gate.column or ""
        numeric = family(cols.get(name)) == "numeric"
        values = [_literal(float(v) if numeric and isinstance(v, str) else v) for v in gate.values]
        return case(_and([not_null(name), exp.Not(this=exp.In(this=col(name), expressions=values))]))
    if gate.type == "range":
        name = gate.column or ""
        bounds: list[exp.Expression] = []
        if gate.min is not None:
            bounds.append(exp.LT(this=col(name), expression=_literal(gate.min)))
        if gate.max is not None:
            bounds.append(exp.GT(this=col(name), expression=_literal(gate.max)))
        out_of_range = bounds[0] if len(bounds) == 1 else exp.Or(this=bounds[0], expression=bounds[1])
        ifs = [exp.If(this=out_of_range, true=one)]
        if not gate.allow_null:
            ifs.insert(0, exp.If(this=is_null(name), true=one))
        return exp.Case(ifs=ifs, default=zero)
    raise InvalidInput(f"{gate.type} is not a row gate")


def compile_outputs(validated: ValidatedRecipe, dialect: str, *, pretty: bool = False) -> dict[str, str]:
    """{output name: SQL} for every output of the recipe."""
    c = Compiler(validated, dialect)
    return {o.name: c.sql(c.output_query(o.id), pretty=pretty) for o in validated.outputs()}
