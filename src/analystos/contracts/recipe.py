"""Transformation recipe IR (ADR-0023, P6-04): one typed DAG, validated before any compiler sees it.

A recipe is an ordered list of nodes; a node reads only nodes listed before it, so the order is a
topological order and a cycle cannot be written. Every node carries an output schema: `source` and
`output` nodes declare it (they are the contract boundaries), every node that creates a column
declares that column's type, and the validator derives the rest and checks any schema a node
declares. Expressions are sqlglot trees in sqlglot's own dialect (the same parser the gateway uses),
stored as their canonical SQL text and transpiled per engine by the compilers; never strings glued
into SQL.

The validator rejects, with every problem listed:
* a column referenced before it exists (unknown column in an expression, key, gate, rename ...);
* an implicit type change: a derived expression whose inferred type differs from its declared type,
  a comparison or arithmetic across type families (`amount = '10'`, `order_date >= '2024-01-01'`),
  a union of differently typed inputs, join keys of different families. An explicit `cast` node or
  a `CAST(...)` in the expression is how a type changes;
* functions sqlglot does not know, aggregates or windows outside `aggregate`/`window`, subqueries.

Models may *propose* recipes (P6-08); this validator and the compilers decide (CLAUDE.md rule 3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import sqlglot
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlglot import exp
from sqlglot.errors import SqlglotError

from analystos.core.errors import InvalidInput
from analystos.core.ids import stable_hash

IR_DIALECT = ""  # sqlglot's own dialect: typed division, standard CAST; compilers transpile from it
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,55}$")  # recipe names and node ids
_COLUMN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ASSET = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
RESERVED_PREFIXES = ("aos_", "__")  # compiler-internal columns (row numbers, gate flags)

Cardinality = Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
Severity = Literal["fail", "warn", "drop"]
SchemaPolicy = Literal["evolve", "warn", "strict"]

# ---------------------------------------------------------------------------------------- types
_TEXT = {"TEXT", "VARCHAR", "CHAR", "NCHAR", "NVARCHAR", "NAME", "BPCHAR", "STRING"}
_SIMPLE = {"INT": "integer", "BIGINT": "bigint", "SMALLINT": "smallint", "TINYINT": "smallint", "DOUBLE": "double",
           "FLOAT": "double", "BOOLEAN": "boolean", "DATE": "date", "TIMESTAMP": "timestamp", "DATETIME": "timestamp",
           "TIMESTAMPNTZ": "timestamp", "TIMESTAMPTZ": "timestamptz", "TIMESTAMPLTZ": "timestamptz"}
NUMERIC_TYPES = {"smallint", "integer", "bigint", "double"}
FAMILIES = {"text": "text", "boolean": "boolean", "date": "temporal", "timestamp": "temporal", "timestamptz": "temporal"}


def canonical_type(value: str | exp.DataType | None, *, dialect: str = "postgres") -> str | None:
    """`text | smallint | integer | bigint | double | numeric[(p,s)] | boolean | date | timestamp |
    timestamptz`, or None when the type is unknown or outside the IR."""
    if value is None:
        return None
    try:
        dt = value if isinstance(value, exp.DataType) else exp.DataType.build(str(value), dialect=dialect)
    except (SqlglotError, ValueError):
        return None
    name = dt.this.name if hasattr(dt.this, "name") else str(dt.this)
    if name in _TEXT:
        return "text"
    if name in ("DECIMAL", "NUMERIC", "BIGDECIMAL"):
        params = [p.this.name for p in dt.expressions if isinstance(p, exp.DataTypeParam) and isinstance(p.this, exp.Literal)]
        return f"numeric({params[0]},{params[1] if len(params) > 1 else 0})" if params else "numeric"
    return _SIMPLE.get(name)


def family(ctype: str | None) -> str | None:
    if ctype is None:
        return None
    if ctype in NUMERIC_TYPES or ctype.startswith("numeric"):
        return "numeric"
    return FAMILIES.get(ctype)


def sql_type(ctype: str) -> exp.DataType:
    """The sqlglot DataType of a canonical type, for CAST in any dialect."""
    if ctype == "double":
        return exp.DataType.build("DOUBLE")
    if ctype == "integer":
        return exp.DataType.build("INT")
    return exp.DataType.build(ctype, dialect="postgres")


def same_type(declared: str, inferred: str | None) -> bool:
    if inferred is None:
        return False
    if declared == inferred:
        return True
    return declared == "numeric" and inferred.startswith("numeric")


# ---------------------------------------------------------------------------------------- model
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _ir_type(v: str) -> str:
    ctype = canonical_type(v)
    if ctype is None:
        raise ValueError(f"unsupported type {v!r} (text, smallint, integer, bigint, double, numeric(p,s), boolean, "
                         "date, timestamp, timestamptz)")
    return ctype


IRType = Annotated[str, AfterValidator(_ir_type)]  # a canonical IR type name


class Column(_Model):
    name: str
    type: IRType


class OrderKey(_Model):
    column: str
    desc: bool = False


class _Node(_Model):
    id: str
    output_schema: list[Column] | None = Field(default=None, alias="schema")

    def inputs(self) -> list[str]:
        return []


class SourceNode(_Node):
    op: Literal["source"] = "source"
    asset: str  # "schema.table" as the gateway sees it
    snapshot: str | None = None  # a content fingerprint to pin (recorded; a mismatch refuses the run)


class SelectNode(_Node):
    op: Literal["select"] = "select"
    input: str
    columns: list[str]

    def inputs(self) -> list[str]:
        return [self.input]


class FilterNode(_Node):
    op: Literal["filter"] = "filter"
    input: str
    predicate: str

    def inputs(self) -> list[str]:
        return [self.input]


class CastNode(_Node):
    op: Literal["cast"] = "cast"
    input: str
    casts: dict[str, IRType]

    def inputs(self) -> list[str]:
        return [self.input]


class Derivation(_Model):
    name: str
    expr: str
    type: IRType


class DeriveNode(_Node):
    op: Literal["derive"] = "derive"
    input: str
    columns: list[Derivation]

    def inputs(self) -> list[str]:
        return [self.input]


class RenameNode(_Node):
    op: Literal["rename"] = "rename"
    input: str
    mapping: dict[str, str]

    def inputs(self) -> list[str]:
        return [self.input]


class DedupeNode(_Node):
    op: Literal["dedupe"] = "dedupe"
    input: str
    keys: list[str]
    order: list[OrderKey] = Field(default_factory=list)  # first row per key wins; ties broken by every column

    def inputs(self) -> list[str]:
        return [self.input]


class JoinKey(_Model):
    left: str
    right: str


class JoinNode(_Node):
    op: Literal["join"] = "join"
    left: str
    right: str
    how: Literal["inner", "left"] = "inner"
    on: list[JoinKey]
    expected_cardinality: Cardinality

    def inputs(self) -> list[str]:
        return [self.left, self.right]


class Measure(_Model):
    name: str
    func: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    column: str | None = None  # None only for count (COUNT(*))
    type: IRType


class AggregateNode(_Node):
    op: Literal["aggregate"] = "aggregate"
    input: str
    keys: list[str] = Field(default_factory=list)
    measures: list[Measure]

    def inputs(self) -> list[str]:
        return [self.input]


class WindowColumn(_Model):
    name: str
    func: Literal["row_number", "rank", "dense_rank", "lag", "lead", "sum", "avg", "min", "max", "count"]
    column: str | None = None
    offset: int = 1  # lag/lead
    partition_by: list[str] = Field(default_factory=list)
    order_by: list[OrderKey] = Field(default_factory=list)
    type: IRType


class WindowNode(_Node):
    op: Literal["window"] = "window"
    input: str
    columns: list[WindowColumn]

    def inputs(self) -> list[str]:
        return [self.input]


class UnionNode(_Node):
    op: Literal["union"] = "union"
    union_inputs: list[str] = Field(alias="inputs", min_length=2)
    distinct: bool = False

    def inputs(self) -> list[str]:
        return list(self.union_inputs)


class Gate(_Model):
    """A data-quality gate on an output (P6-05). Row gates (not_null, unique, accepted_values, range)
    may `drop` failing rows into quarantine; table gates (row_count_delta) may only fail or warn."""

    type: Literal["not_null", "unique", "accepted_values", "range", "row_count_delta"]
    severity: Severity = "fail"
    column: str | None = None
    columns: list[str] = Field(default_factory=list)  # unique over several columns
    values: list[Any] = Field(default_factory=list)  # accepted_values
    min: float | int | None = None
    max: float | int | None = None
    allow_null: bool = True  # range: a NULL passes unless False
    max_change_pct: float | None = None  # row_count_delta against the last good output
    name: str | None = None

    def key(self) -> str:
        if self.name:
            return self.name
        cols = ",".join(self.columns or ([self.column] if self.column else []))
        return f"{self.type}({cols})"

    def targets(self) -> list[str]:
        return list(self.columns) if self.columns else ([self.column] if self.column else [])


ROW_GATES = {"not_null", "unique", "accepted_values", "range"}


class OutputNode(_Node):
    op: Literal["output"] = "output"
    input: str
    name: str  # the output's name (its materialized table is named after the recipe and this)
    grain: list[str] = Field(default_factory=list)  # the columns one row is about
    keys: list[str] = Field(default_factory=list)  # unique, non-null: implies fail gates
    gates: list[Gate] = Field(default_factory=list)
    schema_policy: SchemaPolicy = "warn"

    def inputs(self) -> list[str]:
        return [self.input]

    def all_gates(self) -> list[Gate]:
        """Declared gates plus the ones the keys imply (unique + not_null, severity fail)."""
        implied: list[Gate] = []
        if self.keys:
            declared = {g.key() for g in self.gates}
            u = Gate(type="unique", columns=list(self.keys), severity="fail", name=f"key_unique({','.join(self.keys)})")
            if u.key() not in declared:
                implied.append(u)
            for k in self.keys:
                g = Gate(type="not_null", column=k, severity="fail", name=f"key_not_null({k})")
                if g.key() not in declared:
                    implied.append(g)
        return [*implied, *self.gates]


Node = Annotated[SourceNode | SelectNode | FilterNode | CastNode | DeriveNode | RenameNode | DedupeNode | JoinNode |
                 AggregateNode | WindowNode | UnionNode | OutputNode, Field(discriminator="op")]


class Recipe(_Model):
    kind: Literal["Recipe"] = "Recipe"
    name: str
    description: str = ""
    nodes: list[Node] = Field(min_length=2)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError("recipe name must match [a-z][a-z0-9_]{0,55}")
        return v

    @model_validator(mode="after")
    def _unique_ids(self) -> Recipe:
        seen: set[str] = set()
        for n in self.nodes:
            if n.id in seen:
                raise ValueError(f"duplicate node id {n.id}")
            seen.add(n.id)
        return self

    def spec(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def hash(self) -> str:
        return stable_hash(self.spec())


# ---------------------------------------------------------------------------------------- validation
class RecipeInvalid(InvalidInput):
    code = "recipe_invalid"

    def __init__(self, problems: list[str]) -> None:
        super().__init__("recipe rejected: " + "; ".join(problems[:10]), details={"problems": problems})
        self.problems = problems


@dataclass
class ValidatedRecipe:
    recipe: Recipe
    schemas: dict[str, list[Column]]  # node id -> output schema (declared or derived, always present)
    exprs: dict[tuple[str, str], exp.Expression] = field(default_factory=dict)  # (node id, name|"predicate") -> tree
    nodes: dict[str, Any] = field(default_factory=dict)

    @property
    def hash(self) -> str:
        """Hash of the stamped form, so a recipe with or without its derived schemas is the same recipe."""
        return stable_hash(self.stamped())

    def outputs(self) -> list[OutputNode]:
        return [n for n in self.recipe.nodes if isinstance(n, OutputNode)]

    def sources(self) -> list[SourceNode]:
        return [n for n in self.recipe.nodes if isinstance(n, SourceNode)]

    def ancestors(self, node_id: str) -> list[str]:
        """`node_id` and everything it reads, in recipe order."""
        need, stack = set(), [node_id]
        while stack:
            nid = stack.pop()
            if nid in need:
                continue
            need.add(nid)
            stack.extend(self.nodes[nid].inputs())
        return [n.id for n in self.recipe.nodes if n.id in need]

    def columns(self, node_id: str) -> dict[str, str]:
        return {c.name: c.type for c in self.schemas[node_id]}

    def stamped(self) -> dict[str, Any]:
        """The recipe with every node's output schema filled in (what the IR carries after validation)."""
        spec = self.recipe.spec()
        for node in spec["nodes"]:
            node["schema"] = [c.model_dump() for c in self.schemas[node["id"]]]
        return spec


_BANNED = tuple(getattr(exp, n) for n in ("Select", "Subquery", "Union", "Window", "AggFunc", "Star", "Placeholder",
                                          "Parameter", "Anonymous", "Exists", "Lambda", "Table")
                if isinstance(getattr(exp, n, None), type))
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ)
_ARITH = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)


def parse_expr(text: str) -> exp.Expression:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty expression")
    try:
        node = sqlglot.parse_one(f"SELECT {text}", read=IR_DIALECT)
    except SqlglotError as exc:
        raise ValueError(f"expression does not parse: {str(exc).splitlines()[0][:160]}") from None
    if not isinstance(node, exp.Select) or len(node.expressions) != 1 or node.args.get("from") or node.args.get("from_"):
        raise ValueError("an expression must be one scalar expression")
    expr = node.expressions[0]
    if isinstance(expr, exp.Alias):
        raise ValueError("an expression cannot carry an alias; name it with the derivation's name")
    return expr


def _schema_mapping(cols: dict[str, str]) -> dict[str, dict[str, str]]:
    return {"__in": {name: sql_type(t).sql() for name, t in cols.items()}}


def infer_type(tree: exp.Expression, cols: dict[str, str]) -> str | None:
    """Inferred canonical type of `tree` over input columns `cols` (sqlglot annotate_types)."""
    from sqlglot.optimizer.annotate_types import annotate_types
    from sqlglot.schema import MappingSchema

    select = exp.select(exp.alias_(tree.copy(), "x")).from_("__in")
    for col in select.find_all(exp.Column):
        col.set("table", exp.to_identifier("__in"))
    try:
        annotated = annotate_types(select, schema=MappingSchema(_schema_mapping(cols), normalize=False))
    except SqlglotError:
        return None
    t = annotated.selects[0].type
    return canonical_type(t) if t is not None and t.this != exp.DataType.Type.UNKNOWN else None


def _typed(node: exp.Expression, cols: dict[str, str]) -> str | None:
    if isinstance(node, exp.Null):
        return None
    return infer_type(node, cols)


def check_expr(tree: exp.Expression, cols: dict[str, str], where: str) -> list[str]:
    """Structural and type checks of one expression over the input columns `cols`."""
    problems: list[str] = []
    for node in tree.walk():
        if isinstance(node, _BANNED):
            kind = "unknown function " + node.name if isinstance(node, exp.Anonymous) else type(node).__name__
            problems.append(f"{where}: {kind} is not allowed in an expression")
        elif isinstance(node, exp.Column):
            if node.table:
                problems.append(f"{where}: column {node.sql()} must be unqualified")
            elif node.name not in cols:
                problems.append(f"{where}: column {node.name} does not exist here (have: {', '.join(sorted(cols))})")
    if problems:
        return problems
    for node in tree.walk():
        if isinstance(node, (*_COMPARISONS, exp.Between, exp.In)):
            left = node.this
            rights = ([node.args["low"], node.args["high"]] if isinstance(node, exp.Between)
                      else list(node.expressions) if isinstance(node, exp.In) else [node.expression])
            lt = family(_typed(left, cols))
            for r in rights:
                rt = family(_typed(r, cols))
                if lt and rt and lt != rt:
                    problems.append(f"{where}: {node.sql()} compares {lt} with {rt} (implicit type change; CAST one side)")
        elif isinstance(node, _ARITH):
            lt, rt = family(_typed(node.this, cols)), family(_typed(node.expression, cols))
            ok = (lt in (None, "numeric") and rt in (None, "numeric")) or (
                lt == "temporal" and rt == "numeric" and isinstance(node, (exp.Add, exp.Sub))) or (
                lt == "temporal" and rt == "temporal" and isinstance(node, exp.Sub))
            if not ok:
                problems.append(f"{where}: {node.sql()} mixes {lt} and {rt} (implicit type change; CAST first)")
        elif isinstance(node, exp.DPipe):
            for side in (node.this, node.expression):
                f = family(_typed(side, cols))
                if f not in (None, "text"):
                    problems.append(f"{where}: {node.sql()} concatenates a {f} value (CAST it to text)")
    return problems


def _ident(name: str, where: str, problems: list[str]) -> None:
    if not isinstance(name, str) or not _COLUMN.match(name) or name.startswith(RESERVED_PREFIXES):
        problems.append(f"{where}: {name!r} is not a valid column name ([a-z][a-z0-9_]*, not aos_*)")


def _need(cols: dict[str, str], names: list[str], where: str, problems: list[str]) -> None:
    for n in names:
        if n not in cols:
            problems.append(f"{where}: column {n} does not exist here (have: {', '.join(sorted(cols))})")


def _literal_family(v: Any) -> str | None:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "numeric"
    if isinstance(v, str):
        return "text"
    return None


def validate_recipe(recipe: Recipe | dict[str, Any]) -> ValidatedRecipe:
    """The IR validator. Raises RecipeInvalid listing every problem; returns the recipe with a schema
    for every node and the parsed expression trees."""
    if not isinstance(recipe, Recipe):
        try:
            recipe = Recipe.model_validate(recipe)
        except ValueError as exc:
            errors = getattr(exc, "errors", lambda: [])()
            raise RecipeInvalid([f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}" for e in errors]
                                or [str(exc)]) from None
    problems: list[str] = []
    schemas: dict[str, list[Column]] = {}
    exprs: dict[tuple[str, str], exp.Expression] = {}
    nodes = {n.id: n for n in recipe.nodes}
    consumed: set[str] = set()
    output_names: set[str] = set()

    for node in recipe.nodes:
        where = f"node {node.id} ({node.op})"
        if not _IDENT.match(node.id):
            problems.append(f"{where}: node id must match [a-z][a-z0-9_]*")
        missing_inputs = [i for i in node.inputs() if i not in schemas]
        for i in node.inputs():
            if i not in nodes:
                problems.append(f"{where}: input {i} does not exist")
            elif i not in schemas and i in nodes:
                problems.append(f"{where}: input {i} must come before it (the recipe is an ordered DAG)")
            elif isinstance(nodes[i], OutputNode):
                problems.append(f"{where}: an output ({i}) cannot be an input")
            consumed.add(i)
        if missing_inputs:
            schemas[node.id] = list(node.output_schema or [])
            continue
        derived = _derive(node, schemas, exprs, where, problems, output_names)
        if node.output_schema is not None and not isinstance(node, SourceNode):
            declared = [(c.name, c.type) for c in node.output_schema]
            got = [(c.name, c.type) for c in derived]
            if declared != got:
                problems.append(f"{where}: declared schema {_fmt(declared)} differs from what the node produces "
                                f"{_fmt(got)}")
        schemas[node.id] = derived
    if not recipe.nodes or not any(isinstance(n, OutputNode) for n in recipe.nodes):
        problems.append("a recipe needs at least one output node")
    for n in recipe.nodes:
        if not isinstance(n, OutputNode) and n.id not in consumed:
            problems.append(f"node {n.id} ({n.op}) is not read by any node: remove it or route it to an output")
    if problems:
        raise RecipeInvalid(problems)
    return ValidatedRecipe(recipe=recipe, schemas=schemas, exprs=exprs, nodes=nodes)


def _fmt(cols: list[tuple[str, str]]) -> str:
    return "[" + ", ".join(f"{n}:{t}" for n, t in cols) + "]"


def _derive(node: Any, schemas: dict[str, list[Column]], exprs: dict, where: str, problems: list[str],
            output_names: set[str]) -> list[Column]:
    """The output schema of one node from its inputs', recording problems."""
    def cols_of(nid: str) -> dict[str, str]:
        return {c.name: c.type for c in schemas[nid]}

    if isinstance(node, SourceNode):
        if not _ASSET.match(node.asset):
            problems.append(f"{where}: asset must be schema.table (got {node.asset!r})")
        if not node.output_schema:
            problems.append(f"{where}: a source declares its schema (the columns it reads and their types)")
            return []
        seen: set[str] = set()
        for c in node.output_schema:
            if not re.match(r"^[a-z_][a-z0-9_]*$", c.name):
                problems.append(f"{where}: source column {c.name!r} is not a staged identifier")
            if c.name in seen:
                problems.append(f"{where}: column {c.name} declared twice")
            seen.add(c.name)
        return list(node.output_schema)

    if isinstance(node, SelectNode):
        cols = cols_of(node.input)
        _need(cols, node.columns, where, problems)
        if len(set(node.columns)) != len(node.columns):
            problems.append(f"{where}: a column is selected twice")
        return [Column(name=c, type=cols[c]) for c in node.columns if c in cols]

    if isinstance(node, FilterNode):
        cols = cols_of(node.input)
        try:
            tree = parse_expr(node.predicate)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
            return list(schemas[node.input])
        found = check_expr(tree, cols, where)
        problems += found
        if not found and infer_type(tree, cols) != "boolean":
            problems.append(f"{where}: the predicate must be boolean (got {infer_type(tree, cols) or 'unknown'})")
        exprs[(node.id, "predicate")] = tree
        return list(schemas[node.input])

    if isinstance(node, CastNode):
        cols = cols_of(node.input)
        _need(cols, list(node.casts), where, problems)
        out = []
        for name, t in cols.items():
            target = canonical_type(node.casts[name]) if name in node.casts else t
            if target is None:
                problems.append(f"{where}: unsupported type {node.casts[name]!r} for {name}")
                target = t
            out.append(Column(name=name, type=target))
        return out

    if isinstance(node, DeriveNode):
        cols = cols_of(node.input)
        out = [Column(name=n, type=t) for n, t in cols.items()]
        for d in node.columns:
            w = f"{where} column {d.name}"
            _ident(d.name, w, problems)
            if d.name in cols or d.name in [c.name for c in out[len(cols):]]:
                problems.append(f"{w}: {d.name} already exists (derive new names; rename or cast existing ones)")
            try:
                tree = parse_expr(d.expr)
            except ValueError as exc:
                problems.append(f"{w}: {exc}")
                continue
            found = check_expr(tree, cols, w)
            problems += found
            if not found:
                inferred = infer_type(tree, cols)
                if inferred is None:
                    problems.append(f"{w}: the type of {tree.sql()} cannot be inferred; wrap it in CAST(... AS {d.type})")
                elif not same_type(d.type, inferred):
                    problems.append(f"{w}: {tree.sql()} is {inferred} but declared {d.type} (implicit type change; "
                                    f"use CAST(... AS {d.type}))")
            exprs[(node.id, d.name)] = tree
            out.append(Column(name=d.name, type=d.type))
        return out

    if isinstance(node, RenameNode):
        cols = cols_of(node.input)
        _need(cols, list(node.mapping), where, problems)
        out = []
        for name, t in cols.items():
            new = node.mapping.get(name, name)
            if name in node.mapping:
                _ident(new, where, problems)
            out.append(Column(name=new, type=t))
        names = [c.name for c in out]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            problems.append(f"{where}: rename produces duplicate columns {dupes}")
        return out

    if isinstance(node, DedupeNode):
        cols = cols_of(node.input)
        if not node.keys:
            problems.append(f"{where}: dedupe needs keys")
        _need(cols, node.keys, where, problems)
        _need(cols, [o.column for o in node.order], where, problems)
        return list(schemas[node.input])

    if isinstance(node, JoinNode):
        left, right = cols_of(node.left), cols_of(node.right)
        if not node.on:
            problems.append(f"{where}: a join needs at least one key pair")
        for k in node.on:
            if k.left not in left:
                problems.append(f"{where}: left key {k.left} does not exist in {node.left}")
            if k.right not in right:
                problems.append(f"{where}: right key {k.right} does not exist in {node.right}")
            if k.left in left and k.right in right and family(left[k.left]) != family(right[k.right]):
                problems.append(f"{where}: key {k.left} ({left[k.left]}) and {k.right} ({right[k.right]}) are different "
                                "types (implicit type change; cast one side first)")
        right_keys = {k.right for k in node.on}
        out = [Column(name=n, type=t) for n, t in left.items()]
        for n, t in right.items():
            if n in right_keys:
                continue
            if n in left:
                problems.append(f"{where}: column {n} exists on both sides; rename one before the join")
                continue
            out.append(Column(name=n, type=t))
        return out

    if isinstance(node, AggregateNode):
        cols = cols_of(node.input)
        _need(cols, node.keys, where, problems)
        out = [Column(name=k, type=cols[k]) for k in node.keys if k in cols]
        if not node.measures:
            problems.append(f"{where}: an aggregate needs at least one measure")
        for m in node.measures:
            w = f"{where} measure {m.name}"
            _ident(m.name, w, problems)
            if m.name in node.keys or m.name in [c.name for c in out]:
                problems.append(f"{w}: {m.name} is already a column")
            if m.column is None and m.func != "count":
                problems.append(f"{w}: {m.func} needs a column")
            if m.column is not None and m.column not in cols:
                problems.append(f"{w}: column {m.column} does not exist here")
            elif m.column is not None and m.func in ("sum", "avg") and family(cols[m.column]) != "numeric":
                problems.append(f"{w}: {m.func} of a {cols[m.column]} column (implicit type change)")
            if m.func in ("count", "count_distinct") and m.type not in ("bigint", "integer") and not m.type.startswith("numeric"):
                problems.append(f"{w}: a count is an integer type, not {m.type}")
            if m.func in ("min", "max") and m.column in cols and family(cols[m.column]) != family(m.type):
                problems.append(f"{w}: {m.func}({m.column}) is {cols[m.column]}, declared {m.type} (implicit type change)")
            out.append(Column(name=m.name, type=m.type))
        return out

    if isinstance(node, WindowNode):
        cols = cols_of(node.input)
        out = [Column(name=n, type=t) for n, t in cols.items()]
        for w_ in node.columns:
            w = f"{where} column {w_.name}"
            _ident(w_.name, w, problems)
            if w_.name in [c.name for c in out]:
                problems.append(f"{w}: {w_.name} already exists")
            _need(cols, w_.partition_by, w, problems)
            _need(cols, [o.column for o in w_.order_by], w, problems)
            if w_.func in ("row_number", "rank", "dense_rank", "lag", "lead") and not w_.order_by:
                problems.append(f"{w}: {w_.func} needs order_by")
            if w_.func in ("row_number", "rank", "dense_rank"):
                if w_.column is not None:
                    problems.append(f"{w}: {w_.func} takes no column")
                if family(w_.type) != "numeric":
                    problems.append(f"{w}: {w_.func} is an integer, not {w_.type}")
            else:
                if w_.column is None and w_.func != "count":
                    problems.append(f"{w}: {w_.func} needs a column")
                elif w_.column is not None and w_.column not in cols:
                    problems.append(f"{w}: column {w_.column} does not exist here")
                elif w_.column is not None and w_.func in ("lag", "lead", "min", "max") and \
                        family(cols[w_.column]) != family(w_.type):
                    problems.append(f"{w}: {w_.func}({w_.column}) is {cols[w_.column]}, declared {w_.type} "
                                    "(implicit type change)")
                elif w_.column is not None and w_.func in ("sum", "avg") and family(cols[w_.column]) != "numeric":
                    problems.append(f"{w}: {w_.func} of a {cols[w_.column]} column")
            out.append(Column(name=w_.name, type=w_.type))
        return out

    if isinstance(node, UnionNode):
        first = [(c.name, c.type) for c in schemas[node.union_inputs[0]]]
        for other in node.union_inputs[1:]:
            got = [(c.name, c.type) for c in schemas[other]]
            if got != first:
                problems.append(f"{where}: {other} has schema {_fmt(got)}, {node.union_inputs[0]} has {_fmt(first)}; "
                                "union inputs must match exactly (select, rename or cast first)")
        return list(schemas[node.union_inputs[0]])

    if isinstance(node, OutputNode):
        cols = cols_of(node.input)
        _ident(node.name, f"{where} name", problems)
        if node.name in output_names:
            problems.append(f"{where}: output name {node.name} is used twice")
        output_names.add(node.name)
        if not node.output_schema:
            problems.append(f"{where}: an output declares its schema")
            return list(schemas[node.input])
        declared = {c.name: c.type for c in node.output_schema}
        for name in declared:  # gate flags and quarantine bookkeeping use the aos_ namespace
            _ident(name, f"{where} column", problems)
        for name, t in declared.items():
            if name not in cols:
                problems.append(f"{where}: declared column {name} does not exist in {node.input}")
            elif not same_type(t, cols[name]):
                problems.append(f"{where}: column {name} is {cols[name]} but the output declares {t} (implicit type "
                                "change; add a cast node)")
        extra = [c for c in cols if c not in declared]
        if extra:
            problems.append(f"{where}: {node.input} produces columns the output does not declare: {extra} (select them "
                            "away or declare them)")
        _need(declared, node.grain, f"{where} grain", problems)
        _need(declared, node.keys, f"{where} keys", problems)
        for g in node.gates:
            gw = f"{where} gate {g.key()}"
            if g.type in ROW_GATES and not g.targets():
                problems.append(f"{gw}: needs column or columns")
            _need(declared, g.targets(), gw, problems)
            if g.severity == "drop" and g.type not in ROW_GATES:
                problems.append(f"{gw}: only row gates (not_null, unique, accepted_values, range) can drop rows")
            if g.type == "accepted_values":
                if not g.values:
                    problems.append(f"{gw}: accepted_values needs values")
                col_f = family(declared.get(g.column or ""))
                bad = [v for v in g.values if _literal_family(v) not in (None, col_f)]
                if bad:
                    problems.append(f"{gw}: values {bad[:3]} are not {col_f} (implicit type change)")
            if g.type == "range":
                if g.min is None and g.max is None:
                    problems.append(f"{gw}: range needs min or max")
                if family(declared.get(g.column or "")) != "numeric":
                    problems.append(f"{gw}: range applies to numeric columns")
            if g.type == "row_count_delta" and (g.max_change_pct is None or g.max_change_pct < 0):
                problems.append(f"{gw}: row_count_delta needs max_change_pct >= 0")
        return list(node.output_schema)

    problems.append(f"{where}: unsupported node")
    return []
