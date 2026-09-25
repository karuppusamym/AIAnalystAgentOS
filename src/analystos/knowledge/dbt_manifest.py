"""dbt `manifest.json` (v12+) as OKF documents (P4-K06, spec v3 §6.2 "dbt manifest: lineage and model docs").

Reads models, seeds, snapshots and sources with their descriptions, columns, tags and the tests
attached to them, and the lineage between them (`depends_on`, `child_map`). Pure: no database.

What is *not* carried over: SQL bodies (raw or compiled) and test arguments. A test is recorded as
its kind and column (`unique` on `number`), never its arguments, because `accepted_values` and
custom tests put literal values there.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from analystos.core.errors import InvalidInput
from analystos.knowledge import okf
from analystos.knowledge.crawl_docs import cell, safe_segment

DBT_ACTOR = "process:analystos-dbt-ingest"
MIN_SCHEMA_VERSION = 12
NODE_TYPES = ("model", "seed", "snapshot")
_TYPE_TITLES = {"model": "dbt Model", "seed": "dbt Seed", "snapshot": "dbt Snapshot", "source": "dbt Source"}
_VERSION = re.compile(r"/manifest/v(\d+)\.json")
PII_TAGS = {"pii", "sensitive", "restricted"}


@dataclass
class DbtColumn:
    name: str
    description: str = ""
    data_type: str | None = None
    tags: list[str] = field(default_factory=list)
    pii: bool = False
    tests: list[str] = field(default_factory=list)


@dataclass
class DbtNode:
    unique_id: str
    resource_type: str
    name: str
    database: str | None
    schema: str | None
    relation: str  # alias / identifier: the physical name
    description: str = ""
    tags: list[str] = field(default_factory=list)
    materialized: str | None = None
    columns: list[DbtColumn] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)  # table-level tests
    source_name: str | None = None

    @property
    def fq(self) -> str:
        return f"{self.schema}.{self.relation}" if self.schema else self.relation


def schema_version(manifest: dict[str, Any]) -> int | None:
    m = _VERSION.search(str((manifest.get("metadata") or {}).get("dbt_schema_version") or ""))
    return int(m.group(1)) if m else None


def project_name(manifest: dict[str, Any]) -> str:
    return str((manifest.get("metadata") or {}).get("project_name") or "dbt")


def _column(c: dict[str, Any]) -> DbtColumn:
    tags = sorted({str(t) for t in (c.get("tags") or []) + ((c.get("config") or {}).get("tags") or [])})
    meta = {**(c.get("meta") or {}), **((c.get("config") or {}).get("meta") or {})}
    pii = bool(PII_TAGS & {t.lower() for t in tags}) or bool(meta.get("contains_pii") or meta.get("pii"))
    return DbtColumn(name=str(c.get("name")), description=str(c.get("description") or "").strip(),
                     data_type=c.get("data_type"), tags=tags, pii=pii)


def parse_manifest(manifest: dict[str, Any]) -> list[DbtNode]:
    """Nodes (models, seeds, snapshots) and sources, with their tests and lineage. InvalidInput when
    this is not a dbt manifest of schema v12 or later."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), dict):
        raise InvalidInput("not a dbt manifest.json (no `nodes` mapping)")
    version = schema_version(manifest)
    if version is None or version < MIN_SCHEMA_VERSION:
        raise InvalidInput(f"dbt manifest schema v{version or '?'} is not supported (v{MIN_SCHEMA_VERSION}+ is)")
    raw_nodes, raw_sources = manifest.get("nodes") or {}, manifest.get("sources") or {}
    out: dict[str, DbtNode] = {}
    for uid, n in sorted(raw_nodes.items()):
        if n.get("resource_type") not in NODE_TYPES:
            continue
        out[uid] = DbtNode(unique_id=uid, resource_type=n["resource_type"], name=str(n.get("name")), database=n.get("database"),
                           schema=n.get("schema"), relation=str(n.get("alias") or n.get("name")),
                           description=str(n.get("description") or "").strip(),
                           tags=sorted({str(t) for t in n.get("tags") or []}),
                           materialized=(n.get("config") or {}).get("materialized"),
                           columns=[_column(c) for c in (n.get("columns") or {}).values()],
                           depends_on=sorted((n.get("depends_on") or {}).get("nodes") or []))
    for uid, s in sorted(raw_sources.items()):
        out[uid] = DbtNode(unique_id=uid, resource_type="source", name=str(s.get("name")), database=s.get("database"),
                           schema=s.get("schema"), relation=str(s.get("identifier") or s.get("name")),
                           description=str(s.get("description") or s.get("source_description") or "").strip(),
                           tags=sorted({str(t) for t in s.get("tags") or []}),
                           columns=[_column(c) for c in (s.get("columns") or {}).values()], source_name=s.get("source_name"))
    for _uid, t in sorted(raw_nodes.items()):
        if t.get("resource_type") != "test":
            continue
        kind = str((t.get("test_metadata") or {}).get("name") or t.get("name"))
        target = t.get("attached_node") or next((d for d in (t.get("depends_on") or {}).get("nodes") or [] if d in out), None)
        node = out.get(target) if target else None
        if node is None:
            continue
        col = t.get("column_name")
        column = next((c for c in node.columns if c.name == col), None) if col else None
        if column is not None:
            column.tests = sorted({*column.tests, kind})
        elif col:
            node.columns.append(DbtColumn(name=str(col), tests=[kind]))
        else:
            node.tests = sorted({*node.tests, kind})
    child_map = manifest.get("child_map") or {}
    for uid, node in out.items():
        node.depends_on = [d for d in node.depends_on if d in out]
        node.children = sorted(c for c in child_map.get(uid) or [] if c in out)
    return list(out.values())


def node_path(project: str, node: DbtNode) -> str:
    base = f"dbt/{safe_segment(project)}"
    if node.resource_type == "source":
        return f"{base}/sources/{safe_segment(f'{node.source_name}.{node.name}')}.md"
    return f"{base}/{node.resource_type}s/{safe_segment(node.name)}.md"


def _link(from_path: str, to_path: str, text: str) -> str:
    return f"[{text}]({posixpath.relpath(to_path, posixpath.dirname(from_path))})"


def render_documents(manifest: dict[str, Any], nodes: list[DbtNode] | None = None) -> dict[str, str]:
    """{pack path: OKF document} for every node and source of the manifest."""
    nodes = nodes if nodes is not None else parse_manifest(manifest)
    project = project_name(manifest)
    meta = manifest.get("metadata") or {}
    by_id = {n.unique_id: n for n in nodes}
    paths = {n.unique_id: node_path(project, n) for n in nodes}
    out: dict[str, str] = {}
    for n in nodes:
        path = paths[n.unique_id]
        tags = {"dbt", n.resource_type, *(re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") for t in n.tags)}
        if any(c.pii for c in n.columns):
            tags.add("pii")
        fm: dict[str, Any] = {
            "type": _TYPE_TITLES[n.resource_type], "title": n.fq, "status": "draft", "tags": sorted(t for t in tags if t),
            "generated": {"by": DBT_ACTOR}, "resource": f"dbt://{n.unique_id}",
            "sources": [{"id": "manifest", "resource": f"dbt manifest of project {project}",
                         "title": f"dbt {meta.get('dbt_version') or ''} manifest ({schema_version(manifest) and f'v{schema_version(manifest)}'})".strip()}],
            "analystos": {"kind": f"dbt_{n.resource_type}", "origin": "dbt", "trusted": False, "unique_id": n.unique_id,
                          "relation": n.fq, "materialized": n.materialized,
                          "mapped_columns": [f"{n.fq}.{c.name}" for c in n.columns][:100]}}
        if n.description:
            first = n.description.split(". ")[0].strip()
            fm["description"] = first if first.endswith(".") else f"{first}."
        body = ["# Description", "", n.description or "No description in the dbt project.", "",
                f"{_TYPE_TITLES[n.resource_type]} `{n.unique_id}`"
                + (f", materialized as {n.materialized}" if n.materialized else "") + f"; relation `{n.fq}`.", "",
                "# Columns", "", "| column | type | description | tests | tags |", "|---|---|---|---|---|"]
        body += [f"| {cell(c.name)} | {cell(c.data_type)} | {cell(c.description)} | {cell(', '.join(c.tests))} | "
                 f"{cell(', '.join(sorted({*c.tags, *(['pii'] if c.pii else [])})))} |" for c in n.columns] or ["| - | - | - | - | - |"]
        if n.tests:
            body += ["", "# Tests", "", *[f"- {t}" for t in n.tests]]
        up = [_link(path, paths[d], by_id[d].fq) for d in n.depends_on]
        down = [_link(path, paths[c], by_id[c].fq) for c in n.children]
        body += ["", "# Lineage", "", "Upstream: " + (", ".join(up) or "none"), "", "Downstream: " + (", ".join(down) or "none")]
        out[path] = okf.render_document(fm, "\n".join(body))
    return out


def lineage_edges(nodes: list[DbtNode]) -> list[tuple[str, str]]:
    """(upstream relation, downstream relation) for every dependency."""
    by_id = {n.unique_id: n for n in nodes}
    return sorted({(by_id[d].fq, n.fq) for n in nodes for d in n.depends_on})
