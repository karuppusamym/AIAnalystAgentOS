"""dbt Core 1.12 interchange (spec v3 §6.2): import `target/osi_document.json`, export an `osi/` folder.

Verified against dbt-core v1.12.0 (`core/dbt/parser/osi.py`, `constants.py`) and a real `dbt parse`:
- dbt writes `target/osi_document.json` with metricflow's MSI→OSI converter (version "0.1.1", one model
  named `semantic_model`, ANSI_SQL expressions, relationships named `<from>__<to>__<entity>`);
- dbt reads **`*.json`** files under `osi/` (or `osi-paths`), any depth; YAML there is ignored;
- only versions 0.1.0 and 0.1.1 parse; every dataset `source` must resolve to a dbt model of the root
  project as `database.schema.alias`, otherwise the parse fails;
- dbt's own output quotes the relation (`"db"."schema"."table"`) but its importer matches unquoted,
  lower-cased parts, so its own file does not re-import. We store and export the unquoted form;
- dbt's exporter writes physical key columns (`primary_key: [customer_id]`) while its importer matches
  `primary_key`, `unique_keys` and relationship `from_columns` against field *names* (the field is
  `customer` with expression `customer_id`), so the entity is lost and the manifest fails validation.
  This adapter rewrites a key column to the field whose expression is exactly that column, both ways.
"""
from __future__ import annotations

import json
import re
from typing import Any

import sqlglot
from sqlglot import exp

from analystos.contracts.semantic import SemanticModelDoc
from analystos.semantic import ossie

_PART = r'(?:"[^"]+"|`[^`]+`|[A-Za-z0-9_$]+)'
_THREE_PART = re.compile(rf"^{_PART}\.{_PART}\.{_PART}$")
_QUOTED = re.compile(r'^"([^".]+)"$|^`([^`.]+)`$')


def relation_name(source: str) -> str:
    """`"db"."schema"."table"` → `db.schema.table` when every part is a quoted plain name; else unchanged."""
    text = source.strip()
    if not _THREE_PART.match(text):
        return source
    parts = re.findall(_PART, text)
    plain = []
    for p in parts:
        m = _QUOTED.match(p)
        plain.append((m.group(1) or m.group(2)) if m else p)
    return ".".join(plain)


def import_osi_document(document: str | bytes | dict[str, Any]) -> tuple[list[SemanticModelDoc], list[str]]:
    """Parse dbt's osi_document.json. Returns (models, issues); raises OssieError when unusable."""
    doc = json.loads(document) if isinstance(document, (str, bytes)) else dict(document)
    if not isinstance(doc, dict):
        raise ossie.OssieError(["osi_document.json root must be an object"])
    version = str(doc.get("version", ""))
    if version not in ossie.ACCEPTED_VERSIONS:
        raise ossie.OssieError([f"unsupported Ossie version {version!r}; dbt 1.12 and AnalystOS accept "
                                f"{', '.join(ossie.ACCEPTED_VERSIONS)}"])
    issues: list[str] = []
    if version != ossie.OSSIE_VERSION:
        issues.append(f"(root): version {version} read as {ossie.OSSIE_VERSION} (same shape)")
        doc["version"] = ossie.OSSIE_VERSION
    doc, conform_issues = ossie.conform(doc)
    issues += conform_issues
    problems = ossie.schema_problems(doc)
    if problems:
        raise ossie.OssieError(problems)
    issues += ossie.semantic_problems(doc)
    models = ossie.from_ossie(doc)
    for m in models:
        for d in m.datasets:
            d.source = relation_name(d.source)
    models = [keys_as_field_names(m) for m in models]
    for m in models:
        for metric in m.metrics:
            problem = ossie.metric_problem(metric)
            if problem:
                issues.append(f"metric {metric.name}: {problem} (imported as a proposal; fix before approving)")
    return models, issues


def keys_as_field_names(model: SemanticModelDoc) -> SemanticModelDoc:
    """Key columns → the names of the fields that expose them (see the module docstring)."""
    by_column: dict[str, dict[str, str]] = {}
    for d in model.datasets:
        cols: dict[str, str] = {}
        names = {f.name for f in d.fields}
        for f in d.fields:
            expr = f.expressions[0].expression.strip() if f.expressions else ""
            if expr and expr not in names and f.name != expr:
                cols.setdefault(expr, f.name)
        by_column[d.name] = cols

    def fix(dataset: str, columns: list[str] | None) -> list[str] | None:
        if columns is None:
            return None
        return [by_column.get(dataset, {}).get(c, c) for c in columns]

    datasets = [d.model_copy(update={"primary_key": fix(d.name, d.primary_key),
                                     "unique_keys": [fix(d.name, k) for k in d.unique_keys] if d.unique_keys is not None else None})
                for d in model.datasets]
    relationships = [r.model_copy(update={"from_columns": fix(r.from_dataset, r.from_columns),
                                          "to_columns": fix(r.to, r.to_columns)}) for r in model.relationships]
    return model.model_copy(update={"datasets": datasets, "relationships": relationships})


def dbt_issues(model: SemanticModelDoc) -> list[str]:
    """What dbt 1.12 would refuse or silently change when it parses this model from `osi/`."""
    out = []
    for d in model.datasets:
        if not _THREE_PART.match(d.source.strip()):
            out.append(f"dataset {d.name}: source must be database.schema.alias of a dbt model for dbt to parse it "
                       f"(got {d.source[:80]!r})")
        if d.primary_key and len(d.primary_key) > 1:
            out.append(f"dataset {d.name}: composite primary key; metricflow keeps each column as a separate entity")
    for m in model.metrics:
        if m.dialect != "ANSI_SQL":
            out.append(f"metric {m.name}: primary expression is {m.dialect}; dbt's converter reads ANSI_SQL")
        elif _dot_in_aggregate_argument(m.expression):
            out.append(f"metric {m.name}: dbt 1.12.0 (metricflow 0.213 OSI→MSI) truncates an aggregate's argument after its "
                       f"last '.', so {m.expression[:60]!r} is mangled on parse (seen: AVG(CASE ... 1.0 ELSE 0.0 END) → AVG(0 END))")
    return out


def _dot_in_aggregate_argument(expression: str) -> bool:
    """metricflow's `_extract_agg_info` renders a non-column argument of COUNT/SUM/AVG/MIN/MAX and keeps
    only what follows its last '.' (`SUM(CASE ... 1 ELSE 0 END)` has its own path and is safe)."""
    try:
        tree = sqlglot.parse_one(expression)
    except sqlglot.errors.ParseError:
        return False
    if not isinstance(tree, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
        return False
    arg = tree.this
    if isinstance(tree, exp.Sum) and isinstance(arg, exp.Case):
        return False
    if isinstance(arg, exp.Distinct):
        arg = arg.expressions[0] if len(arg.expressions) == 1 else None
    return arg is not None and not isinstance(arg, (exp.Column, exp.Star)) and "." in arg.sql()


def export_osi_folder(models: list[SemanticModelDoc]) -> tuple[dict[str, str], list[str]]:
    """`osi/<model>.json` files for a dbt project, plus what dbt would object to. Models without a
    dataset are skipped (the schema requires one)."""
    files: dict[str, str] = {}
    issues: list[str] = []
    for m in models:
        if not m.datasets:
            issues.append(f"model {m.name}: no datasets; nothing to export")
            continue
        m = keys_as_field_names(m.model_copy(update={
            "datasets": [d.model_copy(update={"source": relation_name(d.source)}) for d in m.datasets]}))
        doc = ossie.to_ossie([m])
        problems = ossie.schema_problems(doc)
        if problems:
            raise ossie.OssieError(problems)
        issues += ossie.semantic_problems(doc) + dbt_issues(m)
        files[f"osi/{_file_stem(m.name)}.json"] = json.dumps(doc, indent=2) + "\n"
    return files, issues


def _file_stem(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "semantic_model"
