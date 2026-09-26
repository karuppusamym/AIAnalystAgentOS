"""`analystos check-semantics` (ADR-0019 §6, P7-02): validate semantic model files offline, before they
reach a workspace. No database, no source, no model: the same checks approval and the compiler apply.

For each Ossie 0.1.1 document (YAML or JSON; directories are searched for *.yaml, *.yml and *.json that
hold a `semantic_model`):

* the pinned schema and upstream's rules: unique names, relationship dataset references, every SQL
  expression parses for its dialect (`ossie.validate`);
* names: datasets, fields and metrics are identifiers the compiler can address;
* relationship references: the same number of columns on both sides, each a field (or explicit source
  column) of its dataset, and a known cardinality when one is declared;
* cycles: relationships that form a cycle between datasets give two join paths, which the compiler
  refuses; each cycle is reported with its relationships;
* metric definitions (`review.definition_problems`): expression and filters parse, columns belong to
  the declared dataset, dimensions exist, cross-dataset dimensions have a validated, fan-out-safe path.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from analystos.semantic import ossie

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CARDINALITIES = ("one_to_one", "many_to_one", "one_to_many", "many_to_many")


def _cycles(relationships: list[dict[str, Any]]) -> list[list[str]]:
    """Relationship sets that close a cycle (union-find over datasets; parallel edges count)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    out = []
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for r in sorted(relationships, key=lambda r: r.get("name") or ""):
        a, b = r.get("from"), r.get("to")
        if not a or not b:
            continue
        if find(a) == find(b):
            out.append(sorted({r["name"], *_path_names(adjacency, a, b)}))
        else:
            parent[find(a)] = find(b)
        adjacency.setdefault(a, []).append((b, r["name"]))
        adjacency.setdefault(b, []).append((a, r["name"]))
    return out


def _path_names(adjacency: dict[str, list[tuple[str, str]]], a: str, b: str) -> list[str]:
    stack, seen = [(a, [])], {a}
    while stack:
        node, names = stack.pop()
        if node == b:
            return names
        for nxt, name in adjacency.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, [*names, name]))
    return []


def check_document(doc: dict[str, Any]) -> list[str]:
    """Every problem of one Ossie document (empty = passes)."""
    from analystos.semantic.review import _dataset_columns, definition_problems

    problems = ossie.validate(doc)
    if any(p.startswith("[Schema]") for p in problems):
        return problems
    for model in ossie.from_ossie(doc):
        where = f"model {model.name}"
        datasets = [d.model_dump(mode="json") for d in model.datasets]
        relationships = [r.model_dump(mode="json", by_alias=True) for r in model.relationships]
        by_name = {d["name"]: d for d in datasets}
        for d in model.datasets:
            if not _IDENT.match(d.name):
                problems.append(f"[Name] {where}: dataset {d.name!r} is not an identifier")
            for f in d.fields:
                if not _IDENT.match(f.name):
                    problems.append(f"[Name] {where}: field {d.name}.{f.name!r} is not an identifier")
        for r in model.relationships:
            if len(r.from_columns) != len(r.to_columns) or not r.from_columns:
                problems.append(f"[Reference] relationship {r.name}: from_columns and to_columns differ in length")
            for ds_name, cols in ((r.from_dataset, r.from_columns), (r.to, r.to_columns)):
                known = _dataset_columns(by_name[ds_name]) if ds_name in by_name else None
                if known is None:
                    continue
                missing = [c for c in cols if c.lower() not in known]
                if missing:
                    problems.append(f"[Reference] relationship {r.name}: {ds_name} has no field {', '.join(missing)}")
            if r.cardinality is not None and r.cardinality not in CARDINALITIES:
                problems.append(f"[Reference] relationship {r.name}: unknown cardinality {r.cardinality!r}")
        for cycle in _cycles(relationships):
            problems.append(f"[Cycle] {where}: relationships {', '.join(cycle)} form a cycle, so two join paths exist "
                            "and the compiler refuses to choose; remove one or split the model")
        for m in model.metrics:
            if not _IDENT.match(m.name):
                problems.append(f"[Name] {where}: metric {m.name!r} is not an identifier")
            problems += [f"[Metric] {m.name}: {p}" for p in definition_problems(m, datasets, relationships)]
    return problems


def _files(paths: list[str]) -> list[Path]:
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(x for x in p.rglob("*") if x.suffix in (".yaml", ".yml", ".json") and x.is_file())
        else:
            out.append(p)
    return out


def check_paths(paths: list[str]) -> dict[str, list[str]]:
    """{file: problems} for every semantic model file under `paths` (other YAML/JSON files are skipped
    when a directory was given; a named file must be a semantic model)."""
    named = {Path(p) for p in paths if not Path(p).is_dir()}
    report: dict[str, list[str]] = {}
    for f in _files(paths):
        try:
            doc = ossie.parse_text(f.read_bytes())
        except (ossie.OssieError, ValueError, OSError, yaml.YAMLError) as exc:
            if f in named:
                report[str(f)] = [f"[File] {exc}"]
            continue
        if "semantic_model" not in doc:
            if f in named:
                report[str(f)] = ["[File] not an Ossie document (no semantic_model)"]
            continue
        try:
            report[str(f)] = check_document(doc)
        except ossie.OssieError as exc:
            report[str(f)] = list(exc.problems)
    return report


def main(paths: list[str], *, as_json: bool = False) -> int:
    if not paths:
        print("usage: analystos check-semantics PATH [PATH ...]")
        return 2
    report = check_paths(paths)
    failed = {f: p for f, p in report.items() if p}
    if as_json:
        print(json.dumps({"files": report, "ok": not failed}, indent=2))
    else:
        for f, problems in report.items():
            print(f"{'FAIL' if problems else 'ok  '} {f}")
            for p in problems:
                print(f"     {p}")
        print(f"{len(report)} semantic model file(s) checked, {len(failed)} with problems")
    if not report:
        return 2
    return 1 if failed else 0
