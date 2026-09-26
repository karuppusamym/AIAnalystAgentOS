"""dbt project generation and its static guard (P4-E04, spec v3 §7.3).

A run's virtual dataset becomes one dbt model, its verified column facts become dbt tests, and its
KPIs become dbt semantic-layer YAML (semantic model + simple metrics; dbt 1.12 derives the Ossie
`osi_document.json` from it at parse time, so this is also the `osi/` export until P4-K03 lands).

Generation is deterministic: the same dataset, tests and metrics give byte-identical files, so the
project hash an approval binds can be recomputed. `check_files` and `check_manifest` are the static
half of the BuildGateway's guard (the database grants are the other half): only the file shapes this
module emits pass, every model lands in the target schema, and nothing runs hooks, macros or grants.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from analystos.connectors.naming import is_safe_identifier, sanitize_identifier
from analystos.contracts.bi import MetricDef
from analystos.core.errors import InvalidInput, PolicyDenied
from analystos.core.ids import stable_hash

PROFILE_NAME = "analystos_build"
MATERIALIZATIONS = ("table", "view")
TIME_SPINE = "metricflow_time_spine"
TIME_SPINE_SQL = (
    "{{ config(materialized='view') }}\n"
    "SELECT CAST(d AS DATE) AS date_day\n"
    "FROM generate_series(CAST('2000-01-01' AS DATE), CAST('2040-12-31' AS DATE), INTERVAL '1 day') AS d\n"
)
KEY_TYPES = ("id", "identifier", "key")
DIMENSION_TYPES = ("categorical", "boolean", "category", "dimension", "flag", "code")
MAX_DIMENSIONS = 30

_FILE = re.compile(r"^(dbt_project\.yml|models/[a-z_][a-z0-9_]*\.(sql|yml))$")
_JINJA = re.compile(r"\{\{(.*?)\}\}", re.S)
_SOURCE = re.compile(r"^\s*source\(\s*'([a-z_][a-z0-9_]*)'\s*,\s*'([a-z_][a-z0-9_]*)'\s*\)\s*$")
_REF = re.compile(r"^\s*ref\(\s*'([a-z_][a-z0-9_]*)'\s*\)\s*$")
_CONFIG = re.compile(r"^\s*config\(\s*materialized\s*=\s*'([a-z]+)'\s*\)\s*$")
_FORBIDDEN_KEYS = {"pre-hook", "post-hook", "pre_hook", "post_hook", "+pre-hook", "+post-hook", "sql_header",
                   "+sql_header", "grants", "+grants", "database", "+database", "alias", "+alias", "+schema",
                   "on-run-start", "on-run-end", "vars", "dispatch", "packages", "macro-paths", "seed-paths",
                   "snapshot-paths", "analysis-paths", "test-paths", "docs-paths", "asset-paths", "query-comment"}


@dataclass
class GeneratedProject:
    name: str
    model: str
    files: dict[str, str]
    sources: list[str]
    tests: list[dict[str, str]]
    metrics: list[dict[str, str]] = field(default_factory=list)
    skipped_metrics: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return project_hash(self.files)

    def relations(self, target_schema: str) -> list[str]:
        models = sorted(p[len("models/"):-4] for p in self.files if p.endswith(".sql"))
        return [f"{target_schema}.{m}" for m in models]


def project_hash(files: dict[str, str]) -> str:
    """The hash an approval binds: every file path and byte of the project, nothing else (the
    profile with the credentials is written beside the project, never into it)."""
    return stable_hash({"files": dict(sorted(files.items()))})


def _dump(doc: Any) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000)


def project_name_for(dataset_name: str) -> str:
    return sanitize_identifier(f"analystos_{dataset_name}", max_length=60, fallback="analystos_build")


# --------------------------------------------------------------------------------------- model SQL
def model_sql(dataset_sql: str, dialect: str = "postgres") -> tuple[str, list[tuple[str, str]]]:
    """The dataset SELECT with every `schema.table` replaced by `{{ source('schema', 'table') }}`."""
    try:
        tree = sqlglot.parse_one(dataset_sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise InvalidInput(f"the dataset SQL does not parse: {str(exc)[:200]}") from exc
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    found: list[tuple[str, str]] = []
    placeholders: dict[str, str] = {}
    for table in list(tree.find_all(exp.Table)):
        if not table.db and table.name in ctes:
            continue
        if not table.db or table.catalog:
            raise InvalidInput(f"table {table.sql(dialect=dialect)} must be referenced as schema.table")
        key = (table.db, table.name)
        if not (is_safe_identifier(key[0]) and is_safe_identifier(key[1])):
            raise InvalidInput(f"table {key[0]}.{key[1]} is not a safe identifier")
        if key not in found:
            found.append(key)
        token = f"aos_src_{found.index(key)}_"
        placeholders[token] = "{{ source('" + key[0] + "', '" + key[1] + "') }}"
        table.replace(exp.Table(this=exp.to_identifier(token), alias=table.args.get("alias")))
    sql = tree.sql(dialect=dialect, pretty=True)
    for token, ref in placeholders.items():
        sql = re.sub(rf"\b{token}\b", ref, sql)
    return sql + "\n", found


# --------------------------------------------------------------------------------------- tests
def candidate_tests(dataset: dict[str, Any]) -> list[dict[str, str]]:
    """Tests worth checking: not_null + unique on a key column, not_null on the time column. The dry
    run keeps only those the current data satisfies (verified through the query gateway)."""
    from analystos.capabilities.packs import hints
    from analystos.skills.quality import STRICT_KEY_NAMES

    key_names = STRICT_KEY_NAMES | {k.lower() for k in hints().key_columns}  # domain surrogate keys come from packs
    cols = [c for c in dataset.get("columns") or [] if isinstance(c, dict) and c.get("name")]
    names = [c["name"] for c in cols]
    out: list[dict[str, str]] = []
    key = next((c["name"] for c in cols if not c.get("derived") and c["name"].lower() in key_names), None) or \
        next((c["name"] for c in cols if not c.get("derived") and str(c.get("semantic_type") or "") in KEY_TYPES), None)
    if key:
        out += [{"column": key, "test": "not_null"}, {"column": key, "test": "unique"}]
    time_col = dataset.get("time_column")
    if time_col and time_col in names and time_col != key:
        out.append({"column": time_col, "test": "not_null"})
    return out


def test_probe_sql(dataset_sql: str, tests: list[dict[str, str]]) -> str:
    """One read through the gateway that checks every candidate test and counts the rows."""
    parts = ["COUNT(*) AS n"]
    for i, t in enumerate(tests):
        col = '"' + t["column"].replace('"', '""') + '"'
        parts.append(f"COUNT({col}) AS c{i}" if t["test"] == "not_null" else f"COUNT(DISTINCT {col}) AS c{i}")
    return f"SELECT {', '.join(parts)} FROM ({dataset_sql}) d"


def passing_tests(tests: list[dict[str, str]], row: list[Any]) -> list[dict[str, str]]:
    n = int(row[0] or 0)
    return [t for i, t in enumerate(tests) if int(row[i + 1] or 0) == n]


# --------------------------------------------------------------------------------------- metrics
def measure_for(metric: MetricDef, dialect: str = "postgres") -> tuple[str, str, dict[str, Any]] | str:
    """(agg, expr, agg_params) of a MetricFlow measure, or the reason the KPI is not a simple measure."""
    try:
        node = sqlglot.parse_one(metric.sql_expression, read=dialect)
    except sqlglot.errors.ParseError:
        return "the expression does not parse"
    params: dict[str, Any] = {}
    if isinstance(node, exp.WithinGroup) and isinstance(node.this, exp.PercentileCont):
        pct = node.this.this
        order = node.expression
        if not isinstance(pct, exp.Literal) or not isinstance(order, exp.Order) or len(order.expressions) != 1:
            return "unsupported percentile form"
        inner = order.expressions[0].this
        if float(pct.name) == 0.5:
            return "median", inner.sql(dialect=dialect), params
        return "percentile", inner.sql(dialect=dialect), {"percentile": float(pct.name), "use_discrete_percentile": False}
    aggs = {exp.Sum: "sum", exp.Avg: "average", exp.Min: "min", exp.Max: "max"}
    for cls, agg in aggs.items():
        if type(node) is cls:
            return agg, node.this.sql(dialect=dialect), params
    if type(node) is exp.Count:
        arg = node.this
        if isinstance(arg, exp.Star) or arg is None:
            return "count", "1", params
        if isinstance(arg, exp.Distinct):
            if len(arg.expressions) != 1:
                return "COUNT(DISTINCT ...) over several columns"
            return "count_distinct", arg.expressions[0].sql(dialect=dialect), params
        return "count", arg.sql(dialect=dialect), params
    return f"not a single aggregate ({type(node).__name__})"


# --------------------------------------------------------------------------------------- generate
def generate(dataset: dict[str, Any], metrics: list[MetricDef], tests: list[dict[str, str]], *,
             dialect: str = "postgres") -> GeneratedProject:
    name = project_name_for(dataset["name"])
    model = sanitize_identifier(dataset["name"], max_length=60, fallback="dataset")
    body, sources = model_sql(dataset["sql"], dialect)
    columns = [c for c in dataset.get("columns") or [] if isinstance(c, dict) and c.get("name")]
    by_col: dict[str, list[str]] = {}
    for t in tests:
        by_col.setdefault(t["column"], []).append(t["test"])
    col_docs = []
    for c in columns:
        doc: dict[str, Any] = {"name": c["name"], "quote": True}
        label = c.get("label") or c.get("business_name")
        if label:
            doc["description"] = str(label)
        if c["name"] in by_col:
            doc["data_tests"] = list(by_col[c["name"]])
        col_docs.append(doc)
    notes: list[str] = []
    models_doc: list[dict[str, Any]] = [{"name": model, "description": dataset.get("description") or "", "columns": col_docs}]
    schema_doc: dict[str, Any] = {"version": 2, "models": models_doc}
    files: dict[str, str] = {}

    time_col = dataset.get("time_column")
    emitted: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    measures: list[dict[str, Any]] = []
    metric_docs: list[dict[str, Any]] = []
    for m in metrics:
        got = measure_for(m, dialect)
        mname = sanitize_identifier(m.name, max_length=60, fallback="metric")
        if isinstance(got, str):
            skipped.append({"metric": m.name, "reason": got})
            continue
        agg, expr, params = got
        measure = {"name": f"m_{mname}"[:63], "agg": agg, "expr": expr}
        if params:
            measure["agg_params"] = params
        measures.append(measure)
        metric_docs.append({"name": mname, "label": m.display_name or m.name, "description": m.definition or "",
                            "type": "simple", "type_params": {"measure": measure["name"]}})
        emitted.append({"metric": m.name, "dbt_metric": mname, "agg": agg, "expr": expr})
    if measures and not time_col:
        skipped += [{"metric": e["metric"], "reason": "the dataset has no time column for agg_time_dimension"} for e in emitted]
        emitted, measures, metric_docs = [], [], []
    if measures:
        key = next((t["column"] for t in tests if t["test"] == "unique"), None)
        dims = [{"name": time_col, "type": "time", "type_params": {"time_granularity": _granularity(time_col)}}]
        metric_dims = {d for m in metrics for d in m.dimensions}
        for c in columns:
            if len(dims) >= MAX_DIMENSIONS:
                break
            if c["name"] in (time_col, key):
                continue
            if str(c.get("semantic_type") or "") in DIMENSION_TYPES or c["name"] in metric_dims:
                dims.append({"name": c["name"], "type": "categorical", "expr": _quote(c["name"])})
        semantic: dict[str, Any] = {"name": f"sm_{model}"[:63], "model": f"ref('{model}')",
                                    "defaults": {"agg_time_dimension": time_col}}
        if key:
            semantic["entities"] = [{"name": sanitize_identifier(f"{key}_entity"), "type": "primary", "expr": _quote(key)}]
        semantic["dimensions"] = dims
        semantic["measures"] = measures
        schema_doc["semantic_models"] = [semantic]
        schema_doc["metrics"] = metric_docs
        models_doc.insert(0, {"name": TIME_SPINE, "time_spine": {"standard_granularity_column": "date_day"},
                              "columns": [{"name": "date_day", "granularity": "day"}]})
        files[f"models/{TIME_SPINE}.sql"] = TIME_SPINE_SQL
    elif not metrics:
        notes.append("the run has no validated KPIs: no semantic layer was generated")

    by_schema: dict[str, list[str]] = {}
    for schema, table in sources:
        by_schema.setdefault(schema, []).append(table)
    sources_doc = {"version": 2, "sources": [{"name": s, "schema": s, "tables": [{"name": t} for t in sorted(ts)]}
                                             for s, ts in sorted(by_schema.items())]}
    project_doc = {"name": name, "version": "1.0.0", "config-version": 2, "profile": PROFILE_NAME,
                   "model-paths": ["models"], "flags": {"send_anonymous_usage_stats": False},
                   "models": {name: {"+materialized": "table"}}}
    files["dbt_project.yml"] = _dump(project_doc)
    files[f"models/{model}.sql"] = body
    files["models/schema.yml"] = _dump(schema_doc)
    files["models/sources.yml"] = _dump(sources_doc)
    return GeneratedProject(name=name, model=model, files=dict(sorted(files.items())),
                            sources=[f"{s}.{t}" for s, t in sources], tests=list(tests), metrics=emitted,
                            skipped_metrics=skipped, notes=notes)


def _granularity(col: str) -> str:
    for g in ("month", "week", "quarter", "year"):
        if col.endswith(f"_{g}"):
            return g
    return "day"


def _quote(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


# --------------------------------------------------------------------------------------- static guard
def check_files(files: dict[str, str], *, allowed_sources: set[str]) -> None:
    """Fail closed on anything this module would not have generated: other paths (macros, seeds,
    packages), hooks, grants, schema/database overrides, Jinja beyond source/ref/config, and model
    SQL that is not a single read query. `allowed_sources` = the `schema.table`s the run may read."""
    from analystos.gateway.validator import _FORBIDDEN_NODES, _function_denied, _function_names

    problems: list[str] = []
    if "dbt_project.yml" not in files:
        problems.append("dbt_project.yml is missing")
    models = {p[len("models/"):-4] for p in files if p.endswith(".sql")}
    for path, text in files.items():
        if not isinstance(text, str) or not _FILE.match(path):
            problems.append(f"{path}: not a file the builder generates")
            continue
        if path.endswith(".yml"):
            try:
                doc = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                problems.append(f"{path}: not valid YAML")
                continue
            problems += _check_yaml(path, doc, allowed_sources)
            continue
        if "{%" in text or "{#" in text:
            problems.append(f"{path}: Jinja statements are not allowed")
        sql = _render(path, text, models=models, allowed_sources=allowed_sources, problems=problems)
        try:
            stmts = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
        except sqlglot.errors.ParseError:
            problems.append(f"{path}: SQL does not parse")
            continue
        if len(stmts) != 1 or not isinstance(stmts[0], (exp.Select, exp.Union)):
            problems.append(f"{path}: a model must be exactly one SELECT")
            continue
        for node in stmts[0].walk():
            if isinstance(node, _FORBIDDEN_NODES):
                problems.append(f"{path}: {type(node).__name__} is not allowed in a model")
            elif isinstance(node, exp.Func) and any(_function_denied(n) for n in _function_names(node)):
                problems.append(f"{path}: function {node.sql_name()} is not allowed")
    if problems:
        raise PolicyDenied("build project refused: " + "; ".join(sorted(set(problems))[:8]))


def _render(path: str, text: str, *, models: set[str], allowed_sources: set[str], problems: list[str]) -> str:
    """Replace the only Jinja a model may contain (source, ref, config) with plain SQL; record the rest."""

    def sub(m: re.Match) -> str:
        inner = m.group(1)
        if (s := _SOURCE.match(inner)) is not None:
            if f"{s.group(1)}.{s.group(2)}" not in allowed_sources:
                problems.append(f"{path}: source {s.group(1)}.{s.group(2)} is outside the run's scope")
            return f"{s.group(1)}.{s.group(2)}"
        if (r := _REF.match(inner)) is not None:
            if r.group(1) not in models:
                problems.append(f"{path}: ref to unknown model {r.group(1)}")
            return r.group(1)
        if (c := _CONFIG.match(inner)) is not None:
            if c.group(1) not in MATERIALIZATIONS:
                problems.append(f"{path}: materialization {c.group(1)} is not allowed")
            return ""
        problems.append(f"{path}: Jinja expression '{inner.strip()[:60]}' is not allowed")
        return "x"

    return _JINJA.sub(sub, text)


def _check_yaml(path: str, doc: Any, allowed_sources: set[str]) -> list[str]:
    problems: list[str] = []
    if path == "dbt_project.yml":
        extra = set(doc) - {"name", "version", "config-version", "profile", "model-paths", "flags", "models"}
        if extra:
            problems.append(f"dbt_project.yml: keys {sorted(extra)} are not allowed")
        if doc.get("profile") != PROFILE_NAME:
            problems.append("dbt_project.yml: the profile must be the builder's")
        if doc.get("model-paths", ["models"]) != ["models"]:
            problems.append("dbt_project.yml: model-paths must be ['models']")
        if set(doc.get("flags") or {}) - {"send_anonymous_usage_stats"}:
            problems.append("dbt_project.yml: only the send_anonymous_usage_stats flag is allowed")
        for proj in (doc.get("models") or {}).values():
            if not isinstance(proj, dict) or set(proj) - {"+materialized"} or proj.get("+materialized") not in MATERIALIZATIONS:
                problems.append("dbt_project.yml: model configs other than +materialized (table|view) are not allowed")
    elif isinstance(doc, dict):
        for src in doc.get("sources") or []:
            schema = src.get("schema") if isinstance(src, dict) else None
            for t in (src.get("tables") or []) if isinstance(src, dict) else []:
                if f"{schema}.{t.get('name')}" not in allowed_sources:
                    problems.append(f"{path}: source {schema}.{t.get('name')} is outside the run's scope")
            if isinstance(src, dict) and set(src) - {"name", "schema", "tables", "description"}:
                problems.append(f"{path}: source keys other than name/schema/tables are not allowed")
        rest = {k: v for k, v in doc.items() if k != "sources"}
        problems += [f"{path}: key {k} is not allowed" for k in _walk_keys(rest) if k in _FORBIDDEN_KEYS or k == "schema"]
        problems += [f"{path}: config {k} is not allowed" for k in _config_keys(rest) if k not in ("severity", "materialized")]
    return problems


def _walk_keys(node: Any):
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_keys(v)


def _config_keys(node: Any):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "config" and isinstance(v, dict):
                yield from (str(x) for x in v)
            yield from _config_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _config_keys(v)


def check_manifest(manifest: dict[str, Any], *, project: str, target_schema: str, allowed_sources: set[str]) -> dict[str, Any]:
    """The parsed project, as dbt resolved it, must match the approval: every model in the target
    schema, no hooks, grants or project macros, sources only from the run's scope. Returns a summary."""
    problems: list[str] = []
    models, tests = [], []
    for uid, node in (manifest.get("nodes") or {}).items():
        rtype = node.get("resource_type")
        cfg = node.get("config") or {}
        if rtype == "model":
            if node.get("schema") != target_schema:
                problems.append(f"{uid} resolves to schema {node.get('schema')}, not the approved target {target_schema}")
            if cfg.get("pre-hook") or cfg.get("post-hook"):
                problems.append(f"{uid} has hooks")
            if cfg.get("grants"):
                problems.append(f"{uid} has grants")
            if cfg.get("materialized") not in MATERIALIZATIONS:
                problems.append(f"{uid} materializes as {cfg.get('materialized')}")
            models.append({"unique_id": uid, "name": node.get("name"), "schema": node.get("schema"),
                           "materialized": cfg.get("materialized"),
                           "sources": [d for d in (node.get("depends_on") or {}).get("nodes", []) if d.startswith("source.")]})
        elif rtype == "test":
            if cfg.get("store_failures"):
                problems.append(f"{uid} stores failures (a write outside the model)")
            tests.append(uid)
        else:
            problems.append(f"{uid}: resource type {rtype} is not allowed")
    for uid, src in (manifest.get("sources") or {}).items():
        if f"{src.get('schema')}.{src.get('name')}" not in allowed_sources:
            problems.append(f"{uid} reads {src.get('schema')}.{src.get('name')}, outside the run's scope")
    own_macros = [m for m in manifest.get("macros") or {} if m.startswith(f"macro.{project}.")]
    if own_macros:
        problems.append(f"project macros are not allowed: {own_macros[:3]}")
    for key in ("snapshots", "seeds", "exposures", "unit_tests", "saved_queries"):
        if manifest.get(key):
            problems.append(f"{key} are not allowed")
    if problems:
        raise PolicyDenied("build project refused: " + "; ".join(problems[:8]))
    return {"models": models, "tests": sorted(tests), "semantic_models": sorted(manifest.get("semantic_models") or {}),
            "metrics": sorted(manifest.get("metrics") or {}),
            "dbt_schema_version": (manifest.get("metadata") or {}).get("dbt_schema_version"),
            "dbt_version": (manifest.get("metadata") or {}).get("dbt_version")}
