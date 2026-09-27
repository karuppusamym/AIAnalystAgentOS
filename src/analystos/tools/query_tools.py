"""Governed query tools (P7-11; the DataPilot idea, rewritten on the gateway): a named, parameterized SELECT.

A query tool is a `query_tool` definition (ADR-0021): draft -> tested -> published -> retired. Its spec is a
`QueryToolSpec`: one SELECT with `:name` placeholders, a JSON-Schema for the parameters, optional source and a
row cap. Parameters are validated with `jsonschema`, then bound as typed SQL literals in the parsed tree (never
spliced into text), and the statement runs through `QueryGateway` as the caller, under their scope (CLAUDE.md
rule 4). Only a published (or deprecated) version is callable over MCP; a draft or a tested draft is not.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from analystos.core.errors import InvalidInput

PARAM_TYPES = ("string", "integer", "number", "boolean")
MAX_ROWS = 1000


class QueryToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=3, max_length=1000)
    sql: str = Field(min_length=10, max_length=20_000)
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    source_id: str | None = None
    max_rows: int = Field(default=200, ge=1, le=MAX_ROWS)
    test_arguments: dict[str, Any] = Field(default_factory=dict)  # what `test` runs with unless the caller gives others

    @field_validator("parameters")
    @classmethod
    def _schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        if v.get("type") != "object":
            raise ValueError("parameters must be a JSON-Schema object")
        try:
            Draft202012Validator.check_schema(v)
        except SchemaError as exc:
            raise ValueError(f"parameters is not a valid JSON Schema: {exc.message}") from None
        bad = [n for n, p in (v.get("properties") or {}).items() if not isinstance(p, dict) or p.get("type") not in PARAM_TYPES]
        if bad:
            raise ValueError(f"parameter(s) {', '.join(sorted(bad))} must have a scalar type ({', '.join(PARAM_TYPES)})")
        return v


def placeholders(sql: str) -> tuple[Any, list[str]]:
    """(parsed statement, placeholder names). Exactly one SELECT statement."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = [t for t in sqlglot.parse(sql) if t is not None]
    except sqlglot.errors.ParseError as exc:
        raise InvalidInput(f"the query tool's SQL does not parse: {str(exc)[:200]}") from None
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise InvalidInput("a query tool is exactly one SELECT statement")
    found = list(trees[0].find_all(exp.Placeholder))
    names = sorted({str(p.this) for p in found if p.this})
    if any(not p.this for p in found):
        raise InvalidInput("use named placeholders (:name), not positional ones")
    return trees[0], names


def validate_spec(session: Any, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """The `query_tool` definition validator: the SQL's placeholders and the schema's properties are the same set."""
    from pydantic import ValidationError

    try:
        s = QueryToolSpec.model_validate(spec)
    except ValidationError as exc:
        raise InvalidInput("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from None
    _, names = placeholders(s.sql)
    props = set((s.parameters.get("properties") or {}))
    if set(names) != props:
        raise InvalidInput(f"placeholders {sorted(names)} and parameters {sorted(props)} must be the same set")
    return s.model_dump(mode="json")


def bind(spec: dict[str, Any], arguments: dict[str, Any], dialect: str) -> str:
    """Validated arguments bound as typed literals in the parsed tree; the SQL text is regenerated."""
    from jsonschema import Draft202012Validator
    from sqlglot import exp

    s = QueryToolSpec.model_validate(spec)
    errors = sorted(Draft202012Validator(s.parameters).iter_errors(arguments), key=lambda e: list(e.path))
    if errors:
        raise InvalidInput(f"arguments do not match the tool's parameters: {errors[0].message}",
                           details={"errors": [{"loc": ["arguments", *e.path], "msg": e.message} for e in errors[:20]]})
    extra = set(arguments) - set(s.parameters.get("properties") or {})
    if extra:
        raise InvalidInput(f"unknown argument(s) {', '.join(sorted(extra))}")
    tree, names = placeholders(s.sql)
    missing = [n for n in names if n not in arguments]
    if missing:
        raise InvalidInput(f"missing argument(s) {', '.join(missing)}")

    def literal(node: Any) -> Any:
        if isinstance(node, exp.Placeholder) and node.this:
            v = arguments[str(node.this)]
            return exp.null() if v is None else exp.convert(v)
        return node

    return tree.transform(literal).sql(dialect=dialect)


def run(user: Any, workspace_id: str, key: str, spec: dict[str, Any], arguments: dict[str, Any], *, actor: str,
        purpose: str) -> dict[str, Any]:
    """Bind and execute through the gateway under the user's scope (tool gate `sql.execute` applies there)."""
    from analystos.db.base import session_scope
    from analystos.governance.policy import resolve_scope
    from analystos.runtime.context import default_gateway

    s_ = QueryToolSpec.model_validate(spec)
    with session_scope() as s:
        scope = resolve_scope(s, s.merge(user), workspace_id, minimum_role="analyst")
    source_id = s_.source_id or (next(iter(sorted(set(scope.asset_sources.values()))), None)
                                 if len(set(scope.asset_sources.values())) == 1 else None)
    dialect = scope.source_dialects.get(source_id, "postgres") if source_id else "postgres"
    sql = bind(spec, dict(arguments or {}), dialect)
    gw = default_gateway()
    if source_id:
        result = gw.run_sql_for(scope, actor=actor, source_id=source_id)(sql, purpose=purpose[:120], max_rows=s_.max_rows)
    else:
        result = gw.execute(scope, sql, actor=actor, purpose=purpose[:120], max_rows=s_.max_rows)
    return {"tool": key, "sql": sql, "query_id": result.query_id, "columns": result.columns, "rows": result.rows[:s_.max_rows],
            "row_count": result.row_count, "truncated": result.truncated, "result_hash": result.result_hash}


def test(session: Any, user: Any, row: Any, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """The definition tester: the tool runs once through the gateway with the test arguments; any refusal
    (validation, scope, gateway) fails the test and the draft stays a draft."""
    args = dict(arguments if arguments is not None else (row.spec or {}).get("test_arguments") or {})
    out = run(user, row.workspace_id, row.key, row.spec, args, actor=f"user:{user.id}", purpose=f"query_tool.test:{row.key}")
    return {"arguments": args, "query_id": out["query_id"], "row_count": out["row_count"], "result_hash": out["result_hash"],
            "columns": out["columns"]}
