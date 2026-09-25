"""Apache Ossie 0.1.1 adapter (ADR-0013 §5): the only module that knows the Ossie document shape.

Validation mirrors upstream `validation/validate.py` at the pinned commit: JSON Schema (the pinned
`schema/ossie-0.1.1.json`), unique names, relationship references and SQL parseability. Documents
from other producers (dbt 1.12 writes `osi_document.json` with a newer metricflow that may add 0.2
fields such as `datatype`) go through `conform()` first, which drops what 0.1.1 has no slot for and
says so, instead of rejecting the whole document.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from analystos.contracts.semantic import (
    DialectExpression,
    SemanticDataset,
    SemanticField,
    SemanticMetricDef,
    SemanticModelDoc,
    SemanticRelationship,
)

OSSIE_VERSION = "0.1.1"
ACCEPTED_VERSIONS = ("0.1.0", "0.1.1")  # what dbt 1.12 accepts (dbt/constants.py SUPPORTED_OSI_VERSIONS)
SCHEMA_PATH = Path(__file__).parent / "schema" / "ossie-0.1.1.json"
EXTENSION_VENDOR = "COMMON"
EXTENSION_KEY = "analystos"
_EXT_FIELDS = ("display_name", "format", "grain", "filters", "dimensions", "source_columns", "dataset", "datatype")
# Upstream validate.py maps these to sqlglot; the rest (MDX, TABLEAU, MAQL) are not SQL and are not parsed.
_SQLGLOT = {"ANSI_SQL": "", "SNOWFLAKE": "snowflake", "DATABRICKS": "databricks"}
AGGREGATES = (exp.AggFunc, exp.PercentileCont, exp.PercentileDisc)


class OssieError(ValueError):
    """The document is not valid Ossie 0.1.1; `problems` lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:5]))
        self.problems = problems


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def parse_text(text: str | bytes) -> dict[str, Any]:
    """Ossie YAML or JSON (JSON is YAML). Top level must be a mapping."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise OssieError(["document root must be a mapping"])
    return data


def dump_yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)


# ------------------------------------------------------------------------------------ validation
def schema_problems(doc: dict[str, Any]) -> list[str]:
    from jsonschema import Draft202012Validator

    out = []
    for error in Draft202012Validator(schema()).iter_errors(doc):
        path = " -> ".join(str(p) for p in error.absolute_path) or "(root)"
        out.append(f"[Schema] {path}: {error.message}")
    return out


def _dups(names: list[str]) -> list[str]:
    seen, dups = set(), []
    for n in names:
        if n in seen and n not in dups:
            dups.append(n)
        seen.add(n)
    return dups


def semantic_problems(doc: dict[str, Any]) -> list[str]:
    out = []
    for model in doc.get("semantic_model") or []:
        if not isinstance(model, dict):
            continue
        mname = model.get("name", "<unnamed>")
        datasets = [d for d in model.get("datasets") or [] if isinstance(d, dict)]
        out += [f"[Unique] Duplicate dataset name '{d}' in model '{mname}'" for d in _dups([d.get("name") for d in datasets])]
        for d in datasets:
            fields = [f.get("name") for f in d.get("fields") or [] if isinstance(f, dict)]
            out += [f"[Unique] Duplicate field name '{f}' in dataset '{d.get('name')}'" for f in _dups(fields)]
        metrics = [m for m in model.get("metrics") or [] if isinstance(m, dict)]
        out += [f"[Unique] Duplicate metric name '{m}' in model '{mname}'" for m in _dups([m.get("name") for m in metrics])]
        rels = [r for r in model.get("relationships") or [] if isinstance(r, dict)]
        out += [f"[Unique] Duplicate relationship name '{r}' in model '{mname}'" for r in _dups([r.get("name") for r in rels])]
        names = {d.get("name") for d in datasets}
        for r in rels:
            for end in ("from", "to"):
                if r.get(end) and r[end] not in names:
                    out.append(f"[Reference] Relationship '{r.get('name')}' references unknown dataset '{r[end]}'")
        for kind, items in (("field", [(d.get("name"), f) for d in datasets for f in d.get("fields") or []]),
                            ("metric", [(mname, m) for m in metrics])):
            for owner, item in items:
                for de in ((item.get("expression") or {}).get("dialects") or []) if isinstance(item, dict) else []:
                    problem = sql_problem(de.get("expression") or "", de.get("dialect") or "ANSI_SQL")
                    if problem:
                        out.append(f"[SQL] {kind} {owner}.{item.get('name')}: {problem}")
    return out


def validate(doc: dict[str, Any]) -> list[str]:
    """Every problem (empty = valid Ossie 0.1.1). Semantic checks run only on schema-valid documents."""
    problems = schema_problems(doc)
    return problems or semantic_problems(doc)


def sql_problem(expression: str, dialect: str) -> str | None:
    """Upstream rule: parse as an expression, else as `SELECT <expr>`; non-SQL dialects are not parsed."""
    if dialect not in _SQLGLOT:
        return None
    read = _SQLGLOT[dialect] or None
    for text in (expression, f"SELECT {expression}"):
        try:
            sqlglot.parse_one(text, read=read)
            return None
        except sqlglot.errors.ParseError as exc:
            last = str(exc).split("\n")[0]
    return last


def normalize_expression(expression: str, dialect: str = "ANSI_SQL") -> str:
    """Canonical form for duplicate detection (SEM-005): quoting, case and whitespace do not matter.
    Unparseable or non-SQL expressions fall back to whitespace-collapsed lower case."""
    read = _SQLGLOT.get(dialect)
    if read is not None:
        try:
            tree = sqlglot.parse_one(expression, read=read or None)
            for ident in tree.find_all(exp.Identifier):
                ident.set("quoted", False)
            return tree.sql(normalize=True).lower()
        except sqlglot.errors.ParseError:
            pass
    return " ".join(expression.split()).lower()


def metric_problem(defn: SemanticMetricDef) -> str | None:
    """A KPI must be one parseable aggregate expression without subqueries (the SEM-003 shape rule)."""
    if defn.dialect not in _SQLGLOT:
        return None
    try:
        tree = sqlglot.parse_one(defn.expression, read=_SQLGLOT[defn.dialect] or None)
    except sqlglot.errors.ParseError as exc:
        return f"unparseable: {str(exc).splitlines()[0]}"
    if not any(isinstance(n, AGGREGATES) for n in tree.walk()):
        return "not an aggregate expression"
    if any(isinstance(n, (exp.Select, exp.Subquery)) for n in tree.walk()):
        return "subqueries are not allowed in metric expressions"
    for node in tree.walk():
        if isinstance(node, exp.Dot) or (isinstance(node, exp.Column) and not node.name):
            return "malformed column reference"
    return None


# ------------------------------------------------------------------------------------ conform
def _allowed(defname: str) -> set[str]:
    return set(schema()["$defs"][defname]["properties"])


def conform(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fit a document from another producer to 0.1.1: drop keys and enum values the pinned schema has
    no slot for (reported, never silent). A metric's `datatype` is kept in our extension."""
    issues: list[str] = []
    dialects = set(schema()["$defs"]["Dialect"]["enum"])
    vendors = set(schema()["$defs"]["Vendor"]["enum"])
    root_allowed = set(schema()["properties"])

    def prune(obj: dict, defname: str, where: str) -> dict:
        allowed = _allowed(defname)
        extra = sorted(k for k in obj if k not in allowed)
        if extra:
            issues.append(f"{where}: dropped {', '.join(extra)} (not in Ossie {OSSIE_VERSION})")
        return {k: v for k, v in obj.items() if k in allowed}

    def expr(obj: dict, where: str) -> dict | None:
        e = obj.get("expression")
        if not isinstance(e, dict):
            return None
        kept = []
        for de in e.get("dialects") or []:
            if isinstance(de, dict) and de.get("dialect") in dialects:
                kept.append(prune(de, "DialectExpression", where))
            else:
                label = de.get("dialect") if isinstance(de, dict) else de
                issues.append(f"{where}: dropped expression in unsupported dialect {label}")
        return {"dialects": kept} if kept else None

    def exts(obj: dict, where: str) -> None:
        if "custom_extensions" in obj:
            keep = [x for x in obj["custom_extensions"] or [] if isinstance(x, dict) and x.get("vendor_name") in vendors]
            if len(keep) != len(obj["custom_extensions"] or []):
                issues.append(f"{where}: dropped custom_extensions of vendors not in Ossie {OSSIE_VERSION}")
            obj["custom_extensions"] = keep

    out = {k: v for k, v in doc.items() if k in root_allowed}
    for k in sorted(set(doc) - root_allowed):
        issues.append(f"(root): dropped {k} (not in Ossie {OSSIE_VERSION})")
    if "dialects" in out:
        out["dialects"] = [d for d in out["dialects"] or [] if d in dialects]
    if "vendors" in out:
        out["vendors"] = [v for v in out["vendors"] or [] if v in vendors]
    models = []
    for m in doc.get("semantic_model") or []:
        if not isinstance(m, dict):
            continue
        mw = f"model {m.get('name')}"
        model = prune(m, "SemanticModel", mw)
        exts(model, mw)
        datasets = []
        for d in model.get("datasets") or []:
            dw = f"dataset {d.get('name')}"
            ds = prune(d, "Dataset", dw)
            exts(ds, dw)
            fields = []
            for f in ds.get("fields") or []:
                fw = f"field {d.get('name')}.{f.get('name')}"
                field = prune(f, "Field", fw)
                exts(field, fw)
                e = expr(field, fw)
                if e is None:
                    issues.append(f"{fw}: dropped (no expression in a supported dialect)")
                    continue
                field["expression"] = e
                if isinstance(field.get("dimension"), dict):
                    field["dimension"] = prune(field["dimension"], "Dimension", fw)
                fields.append(field)
            if "fields" in ds:
                ds["fields"] = fields
            datasets.append(ds)
        model["datasets"] = datasets
        if "relationships" in model:
            rels = []
            for r in model["relationships"] or []:
                rel = prune(r, "Relationship", f"relationship {r.get('name')}")
                exts(rel, f"relationship {r.get('name')}")
                rels.append(rel)
            model["relationships"] = rels
        if "metrics" in model:
            metrics = []
            for mt in model["metrics"] or []:
                mtw = f"metric {mt.get('name')}"
                datatype = mt.get("datatype")
                metric = prune({k: v for k, v in mt.items() if k != "datatype"}, "Metric", mtw)
                exts(metric, mtw)
                e = expr(metric, mtw)
                if e is None:
                    issues.append(f"{mtw}: dropped (no expression in a supported dialect)")
                    continue
                metric["expression"] = e
                if datatype:
                    metric["custom_extensions"] = _with_extension(metric.get("custom_extensions") or [], {"datatype": datatype})
                metrics.append(metric)
            model["metrics"] = metrics
        models.append(model)
    out["semantic_model"] = models
    return out, issues


# ------------------------------------------------------------------------------------ extension
def _read_extension(exts: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(our fields, the other extensions verbatim)."""
    ours: dict[str, Any] = {}
    others = []
    for x in exts or []:
        if x.get("vendor_name") == EXTENSION_VENDOR:
            try:
                data = json.loads(x.get("data") or "")
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get(EXTENSION_KEY), dict):
                ours.update(data[EXTENSION_KEY])
                continue
        others.append(dict(x))
    return ours, others


def _with_extension(exts: list[dict[str, Any]], fields: dict[str, Any]) -> list[dict[str, Any]]:
    current, others = _read_extension(exts)
    merged = {k: v for k, v in {**current, **fields}.items() if v not in (None, [], "")}
    if not merged:
        return others
    return others + [{"vendor_name": EXTENSION_VENDOR, "data": json.dumps({EXTENSION_KEY: merged}, sort_keys=True)}]


# ------------------------------------------------------------------------------------ conversion
def _expressions(obj: dict[str, Any]) -> list[DialectExpression]:
    return [DialectExpression(dialect=d["dialect"], expression=d["expression"]) for d in obj["expression"]["dialects"]]


def from_ossie(doc: dict[str, Any]) -> list[SemanticModelDoc]:
    """Valid Ossie 0.1.1 document → our models (raises OssieError otherwise)."""
    problems = schema_problems(doc)
    if problems:
        raise OssieError(problems)
    models = []
    for m in doc["semantic_model"]:
        datasets = [SemanticDataset(
            name=d["name"], source=d["source"], primary_key=d.get("primary_key"), unique_keys=d.get("unique_keys"),
            description=d.get("description"), ai_context=d.get("ai_context"), custom_extensions=d.get("custom_extensions") or [],
            fields=[SemanticField(name=f["name"], expressions=_expressions(f), dimension=f.get("dimension"), label=f.get("label"),
                                  description=f.get("description"), ai_context=f.get("ai_context"),
                                  custom_extensions=f.get("custom_extensions") or []) for f in d.get("fields") or []])
            for d in m["datasets"]]
        relationships = [SemanticRelationship.model_validate(r) for r in m.get("relationships") or []]
        metrics = []
        for mt in m.get("metrics") or []:
            ours, others = _read_extension(mt.get("custom_extensions") or [])
            metrics.append(SemanticMetricDef(name=mt["name"], expressions=_expressions(mt), description=mt.get("description"),
                                             ai_context=mt.get("ai_context"), custom_extensions=others,
                                             **{k: v for k, v in ours.items() if k in _EXT_FIELDS}))
        models.append(SemanticModelDoc(name=m["name"], description=m.get("description"), ai_context=m.get("ai_context"),
                                       datasets=datasets, relationships=relationships, metrics=metrics,
                                       custom_extensions=m.get("custom_extensions") or []))
    return models


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Omit absent optionals (None, empty lists) the way upstream producers do; keep "" (dbt writes it)."""
    return {k: v for k, v in d.items() if v is not None and v != []}


def _expr_out(exprs: list[DialectExpression]) -> dict[str, Any]:
    return {"dialects": [{"dialect": e.dialect, "expression": e.expression} for e in exprs]}


def metric_to_ossie(m: SemanticMetricDef) -> dict[str, Any]:
    ext = _with_extension(m.custom_extensions, {k: getattr(m, k) for k in _EXT_FIELDS})
    return _clean({"name": m.name, "expression": _expr_out(m.expressions), "description": m.description,
                   "ai_context": m.ai_context, "custom_extensions": ext})


def model_to_ossie(model: SemanticModelDoc) -> dict[str, Any]:
    return _clean({
        "name": model.name, "description": model.description, "ai_context": model.ai_context,
        "datasets": [_clean({
            "name": d.name, "source": d.source, "primary_key": d.primary_key, "unique_keys": d.unique_keys,
            "description": d.description, "ai_context": d.ai_context,
            "fields": [_clean({"name": f.name, "expression": _expr_out(f.expressions), "dimension": f.dimension, "label": f.label,
                               "description": f.description, "ai_context": f.ai_context,
                               "custom_extensions": f.custom_extensions}) for f in d.fields],
            "custom_extensions": d.custom_extensions}) for d in model.datasets],
        "relationships": [_clean({"name": r.name, "from": r.from_dataset, "to": r.to, "from_columns": r.from_columns,
                                  "to_columns": r.to_columns, "ai_context": r.ai_context,
                                  "custom_extensions": r.custom_extensions}) for r in model.relationships],
        "metrics": [metric_to_ossie(m) for m in model.metrics],
        "custom_extensions": model.custom_extensions,
    })


def to_ossie(models: list[SemanticModelDoc]) -> dict[str, Any]:
    """Our models → an Ossie 0.1.1 document. Datasets are required by the schema (minItems 1)."""
    return {"version": OSSIE_VERSION, "semantic_model": [model_to_ossie(m) for m in models]}
