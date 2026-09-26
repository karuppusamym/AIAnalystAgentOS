"""Recipe -> dbt project, emitted into the existing `build/` project shape (ADR-0023 decision 2).

One model per output: the postgres form of the output statement, with every `schema.table` replaced
by `{{ source(...) }}` by `build/project.model_sql` (sqlglot, not string templates); the output's
gates become dbt data tests where dbt Core has one (not_null, unique on one column, accepted_values)
with the gate's severity. The project passes `build/project.check_files`, the static half of the
BuildGateway guard, before it is returned, so it can go through the same dry run, approval and
runner as a dataset build. DataPilot's f-string emitters (`pipeline_codegen/`) are not ported.
"""
from __future__ import annotations

from typing import Any

from analystos.build.project import PROFILE_NAME, _dump, check_files, model_sql, project_hash, project_name_for
from analystos.contracts.recipe import ValidatedRecipe
from analystos.recipes.compiler import Compiler

_SEVERITY = {"fail": "error", "warn": "warn", "drop": "warn"}


def _tests(output: Any) -> tuple[dict[str, list[Any]], list[str]]:
    by_col: dict[str, list[Any]] = {}
    notes: list[str] = []
    for g in output.all_gates():
        cfg = {"config": {"severity": _SEVERITY[g.severity]}}
        if g.severity == "drop":
            notes.append(f"{g.key()}: drop gates are enforced by the platform runner; dbt reports them as warnings")
        if g.type == "not_null":
            by_col.setdefault(g.column or "", []).append({"not_null": cfg})
        elif g.type == "unique" and len(g.targets()) == 1:
            by_col.setdefault(g.targets()[0], []).append({"unique": cfg})
        elif g.type == "accepted_values":
            by_col.setdefault(g.column or "", []).append({"accepted_values": {"values": list(g.values), **cfg}})
        else:
            notes.append(f"{g.key()}: no dbt Core test without packages; enforced by the platform runner only")
    return by_col, notes


def emit_project(validated: ValidatedRecipe, *, allowed_sources: set[str] | None = None) -> dict[str, Any]:
    """{name, files, hash, models, sources, notes} of the recipe's dbt project, statically checked."""
    recipe = validated.recipe
    compiler = Compiler(validated, "postgres")
    name = project_name_for(recipe.name)
    files: dict[str, str] = {}
    models_doc: list[dict[str, Any]] = []
    found: list[tuple[str, str]] = []
    notes: list[str] = []
    for out in validated.outputs():
        body, sources = model_sql(compiler.sql(compiler.output_query(out.id)), "postgres")
        found += [s for s in sources if s not in found]
        files[f"models/{out.name}.sql"] = body
        tests, gate_notes = _tests(out)
        notes += gate_notes
        cols = []
        for c in out.output_schema or []:
            doc: dict[str, Any] = {"name": c.name, "quote": True, "data_type": c.type}
            if c.name in tests:
                doc["data_tests"] = tests[c.name]
            cols.append(doc)
        models_doc.append({"name": out.name, "description": recipe.description or f"recipe {recipe.name}",
                           "columns": cols})
    by_schema: dict[str, list[str]] = {}
    for schema, table in found:
        by_schema.setdefault(schema, []).append(table)
    files["dbt_project.yml"] = _dump({"name": name, "version": "1.0.0", "config-version": 2, "profile": PROFILE_NAME,
                                      "model-paths": ["models"], "flags": {"send_anonymous_usage_stats": False},
                                      "models": {name: {"+materialized": "table"}}})
    files["models/schema.yml"] = _dump({"version": 2, "models": models_doc})
    files["models/sources.yml"] = _dump({"version": 2, "sources": [
        {"name": s, "schema": s, "tables": [{"name": t} for t in sorted(ts)]} for s, ts in sorted(by_schema.items())]})
    files = dict(sorted(files.items()))
    allowed = allowed_sources if allowed_sources is not None else {f"{s}.{t}" for s, t in found}
    check_files(files, allowed_sources=allowed)
    return {"name": name, "files": files, "hash": project_hash(files), "models": [o.name for o in validated.outputs()],
            "sources": [f"{s}.{t}" for s, t in found], "notes": notes}
