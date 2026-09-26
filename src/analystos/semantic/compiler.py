"""Compile approved single-dataset metrics; refuse unvalidated joins and ambiguous names."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from analystos.contracts.policy import DataScope
from analystos.contracts.semantic import SemanticMetricDef, SemanticQuery
from analystos.core.errors import InvalidInput
from analystos.core.ids import stable_hash
from analystos.gateway.validator import validate_sql

COMPILER_VERSION = "semantic.v1"


@dataclass(frozen=True)
class CompiledMetricQuery:
    sql: str
    provenance: dict[str, Any]


def load_catalog(session, workspace_id: str) -> dict[str, Any] | None:
    from analystos.semantic.service import approved_metrics, current_model

    model = current_model(session, workspace_id)
    if model is None or model.status != "approved":
        return None
    return {"id": model.id, "version": model.version, "hash": model.content_hash,
            "datasets": model.datasets,
            "metrics": {name: {"id": m.id, "version": m.version, "hash": m.content_hash,
                                "definition": m.definition}
                        for name, m in approved_metrics(session, workspace_id).items()}}


def _words(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("_", " ").casefold()).strip()


def match_question(question: str, catalog: dict[str, Any]) -> SemanticQuery | None:
    """Full-question matching: never drop an unrecognized period or filter."""
    text = _words(question.rstrip("?.! "))
    text = re.sub(r"^(?:show me |show |what is |give me )(?:the )?", "", text)
    hits = []
    for name, entry in catalog["metrics"].items():
        defn = entry["definition"]
        aliases = {_words(name), _words(defn.get("display_name") or name)}
        for alias in aliases:
            if text == alias:
                hits.append(SemanticQuery(metrics=[name]))
            elif text.startswith(alias + " by "):
                wanted = text[len(alias) + 4:]
                dims = [d for d in defn.get("dimensions", []) if _words(d) == wanted]
                if len(dims) == 1:
                    hits.append(SemanticQuery(metrics=[name], dimensions=dims))
    unique = {h.model_dump_json(): h for h in hits}
    if len(unique) > 1:
        raise InvalidInput("More than one approved metric matches. Use the metric's unique name.")
    return next(iter(unique.values()), None)


def _parse(text: str, dialect: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(text, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        raise InvalidInput("The approved definition does not parse for this source dialect.") from exc
    if len(statements) != 1 or statements[0] is None:
        raise InvalidInput("A semantic expression must contain exactly one expression.")
    return statements[0]


def compile_query(query: SemanticQuery, catalog: dict[str, Any], scope: DataScope) -> CompiledMetricQuery:
    if len(set(query.metrics)) != len(query.metrics) or len(set(query.dimensions)) != len(query.dimensions):
        raise InvalidInput("Metric and dimension names must be unique.")
    entries = catalog["metrics"]
    if any(name not in entries for name in query.metrics):
        raise InvalidInput("Every requested metric must have an approved definition.")
    definitions = [SemanticMetricDef.model_validate(entries[n]["definition"]) for n in query.metrics]
    datasets = {d.dataset for d in definitions}
    if len(datasets) != 1 or None in datasets:
        raise InvalidInput("Governed metrics must share one declared dataset; cross-dataset joins need cardinality review.")
    dataset_name = next(iter(datasets))
    matches = [d for d in catalog["datasets"] if d["name"] == dataset_name]
    if len(matches) != 1:
        raise InvalidInput("The metric's dataset is missing or ambiguous in the approved model.")
    dataset = matches[0]
    # Select the dialect from the physical tables through the gateway, never from a model proposal.
    dialects = set(scope.source_dialects.get(s, "postgres") for s in scope.source_ids)
    if len(dialects) != 1:
        raise InvalidInput("Select a single source dialect for a governed metric query.")
    dialect = next(iter(dialects))
    source = _parse(dataset["source"], dialect)
    if not isinstance(source, exp.Select | exp.SetOperation):
        if not isinstance(source, exp.Column | exp.Identifier | exp.Table):
            raise InvalidInput("A dataset source must be a table or a read query.")
        source = _parse("SELECT * FROM " + dataset["source"], dialect)
    if any(source.find_all(exp.Join)):
        raise InvalidInput("The dataset contains joins whose cardinality has not been certified for governed metrics.")
    validate_sql(scope, source.sql(dialect=dialect), max_rows=query.limit)
    fields = {f["name"]: f for f in dataset.get("fields", [])}
    if len(fields) != len(dataset.get("fields", [])):
        raise InvalidInput("Dataset field names must be unique.")

    def expression(text: str) -> exp.Expression:
        value = _parse(text, dialect)
        if isinstance(value, exp.Query) or any(value.find_all(exp.Subquery)):
            raise InvalidInput("Metric and dimension expressions cannot contain subqueries.")
        for col in value.find_all(exp.Column):
            if col.name not in fields or col.table not in ("", "d", dataset_name):
                raise InvalidInput(f"Undeclared dataset column: {col.sql()}.")
            col.set("table", exp.to_identifier("d"))
        return value

    selected, groups = [], []
    for dim in query.dimensions:
        if dim not in fields or any(dim not in d.dimensions for d in definitions):
            raise InvalidInput(f"Dimension {dim!r} is not approved for every requested metric.")
        field = fields[dim]
        if field.get("dimension") is None:
            raise InvalidInput(f"Field {dim!r} is not declared as a dimension.")
        group = expression(field["expressions"][0]["expression"])
        if any(group.find_all(exp.AggFunc)):
            raise InvalidInput("Dimensions cannot contain aggregates.")
        groups.append(group)
        selected.append(group.copy().as_(dim, quoted=True))
    # Per-metric filters cannot be silently shared across metrics with different populations.
    if len({tuple(d.filters) for d in definitions}) != 1:
        raise InvalidInput("These metrics have different filters. Ask for them separately.")
    for name, defn in zip(query.metrics, definitions, strict=True):
        if defn.grain and _words(defn.grain) not in {"all", "overall"}:
            raise InvalidInput("This metric declares a grain that this compiler cannot yet enforce.")
        if defn.dialect.upper() not in {"ANSI_SQL", dialect.upper()}:
            raise InvalidInput("The metric expression dialect does not match the selected source.")
        value = expression(defn.expression)
        if not any(value.find_all(exp.AggFunc)) or any(value.find_all(exp.Window)):
            raise InvalidInput("Governed metric expressions must aggregate their dataset without windows.")
        selected.append(value.as_(name, quoted=True))
    statement = exp.select(*selected).from_(source.subquery("d"))
    if groups:
        statement = statement.group_by(*groups).order_by(*(g.copy() for g in groups))
    for predicate in definitions[0].filters:
        condition = expression(predicate)
        if any(condition.find_all(exp.AggFunc)):
            raise InvalidInput("Metric filters must apply before aggregation.")
        statement = statement.where(condition)
    statement = statement.limit(query.limit)
    validated = validate_sql(scope, statement.sql(dialect=dialect), max_rows=query.limit)
    provenance = {"model_id": catalog["id"], "model_version": catalog["version"], "model_hash": catalog["hash"],
                  "compiler_version": COMPILER_VERSION, "query": query.model_dump(),
                  "metrics": [{"name": n, **{k: entries[n][k] for k in ("id", "version", "hash")}} for n in query.metrics],
                  "policy_version": scope.policy_version, "scope_hash": scope.scope_hash(),
                  "sql_hash": stable_hash(validated.executable_sql)}
    return CompiledMetricQuery(validated.executable_sql, provenance)
