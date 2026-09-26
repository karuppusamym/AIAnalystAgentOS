"""Column lineage of a dbt project from its manifest (P6-07).

Idea from Atlas `dbt_column_lineage.py` (AIDataAnalyst@8b48fd9:src/aida/dbt_column_lineage.py; ADR-0018
verdict IDEA): run the SQL lineage parser over each model's compiled SQL and map every source table
it names back to one of the model's declared dependencies (a dbt `unique_id`). What changes is the
parser under it (`evidence/lineage/sql.py`, which traces through CTEs) and catalog columns: pass the
columns of the dependencies (from dbt's `catalog.json`, or the platform's own catalog) and unqualified
columns resolve too. Without them an unqualified column is never guessed back to "the only
dependency", and an edge to a table that is not a declared dependency is dropped, not fabricated.
Compiled SQL is redacted before it is kept.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from analystos.evidence.lineage.sql import TransformationType, parse_lineage, redact_literals, sql_hash

MAX_COLUMN_EDGES_PER_RESOURCE = 2_000
MAX_COMPILED_SQL_CHARS = 2_000_000
_COLUMN_NAME_LIMIT = 255
RESOURCE_TYPES = ("model", "seed", "snapshot", "source")


@dataclass(frozen=True, slots=True)
class DbtResource:
    """What lineage needs of one manifest resource (model, seed, snapshot or source)."""

    unique_id: str
    name: str
    relation_name: str | None = None
    database_name: str | None = None
    schema_name: str | None = None
    compiled_sql_redacted: str | None = None
    compiled_sql_hash: str | None = None
    sql_parse_status: str = "NOT_PRESENT"  # PARSED | UNPARSEABLE | TOO_LARGE | NOT_PRESENT
    depends_on_unique_ids: tuple[str, ...] = ()
    column_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ColumnLineageEdge:
    source_unique_id: str
    source_column: str
    target_unique_id: str
    target_column: str
    transformation_type: str
    confidence: str


def _norm(name: str | None) -> str | None:
    if not name:
        return None
    return ".".join(p.strip().strip('"`[]').lower() for p in name.split("."))


def _keys(dep: DbtResource) -> list[str]:
    keys = [k for k in (_norm(dep.relation_name),
                        _norm(".".join(p for p in (dep.database_name, dep.schema_name, dep.name) if p))) if k]
    if dep.schema_name:
        keys.append(_norm(f"{dep.schema_name}.{dep.name}") or "")
    if not dep.relation_name and not dep.database_name and not dep.schema_name:
        keys.append(_norm(dep.name) or "")
    return [k for k in dict.fromkeys(keys) if k]


def _match(table: str, deps: Sequence[DbtResource]) -> str | None:
    """The unique_id of the dependency `table` (as named in the compiled SQL) refers to."""
    key = _norm(table)
    found = [d.unique_id for d in deps if key in _keys(d)]
    return found[0] if len(set(found)) == 1 else None


def _catalog(deps: Sequence[DbtResource], columns: dict[str, Sequence[str]] | None) -> dict[str, list[str]] | None:
    if not columns:
        return None
    out = {}
    for d in deps:
        cols = list(columns.get(d.unique_id) or d.column_names or [])
        if not cols:
            continue
        name = d.relation_name or ".".join(p for p in (d.database_name, d.schema_name, d.name) if p) or d.name
        out[_norm(name) or d.name] = cols
    return out or None


def extract_column_lineage(resource: DbtResource, dependencies: Sequence[DbtResource], dialect: str = "postgres", *,
                           catalog_columns: dict[str, Sequence[str]] | None = None) -> list[ColumnLineageEdge]:
    """Column edges of one resource onto its declared dependencies (bounded per resource)."""
    if resource.sql_parse_status != "PARSED" or not resource.compiled_sql_redacted:
        return []
    declared = set(resource.depends_on_unique_ids)
    deps = [d for d in dependencies if d.unique_id in declared]
    if not deps:
        return []
    parsed = parse_lineage(resource.compiled_sql_redacted, dialect, catalog=_catalog(deps, catalog_columns))
    edges: list[ColumnLineageEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for e in parsed.edges:
        if not e.source_resolved or e.transformation_type in (TransformationType.TABLE_STAR.value,
                                                               TransformationType.FILTERED.value):
            continue
        uid = _match(e.source_table, deps)
        if uid is None:
            continue
        key = (uid, e.source_column[:_COLUMN_NAME_LIMIT], e.target_column[:_COLUMN_NAME_LIMIT])
        if key in seen:
            continue
        seen.add(key)
        edges.append(ColumnLineageEdge(source_unique_id=uid, source_column=key[1], target_unique_id=resource.unique_id,
                                       target_column=key[2], transformation_type=e.transformation_type,
                                       confidence=e.confidence))
        if len(edges) >= MAX_COLUMN_EDGES_PER_RESOURCE:
            break
    return edges


def resources_from_manifest(manifest: dict[str, Any]) -> dict[str, DbtResource]:
    """Every model, seed, snapshot and source of a manifest, compiled SQL redacted on the way in."""
    out: dict[str, DbtResource] = {}
    for section in ("nodes", "sources"):
        for uid, node in (manifest.get(section) or {}).items():
            if node.get("resource_type") not in RESOURCE_TYPES:
                continue
            code = node.get("compiled_code") or node.get("compiled_sql")
            if not code:
                status, redacted, digest = "NOT_PRESENT", None, None
            elif len(code) > MAX_COMPILED_SQL_CHARS:
                status, redacted, digest = "TOO_LARGE", None, sql_hash(code[:MAX_COMPILED_SQL_CHARS])
            else:
                redacted, digest = redact_literals(code), sql_hash(code)
                status = "PARSED" if not parse_lineage(code).errors else "UNPARSEABLE"
            out[uid] = DbtResource(
                unique_id=uid, name=node.get("alias") or node.get("identifier") or node.get("name") or uid,
                relation_name=node.get("relation_name"), database_name=node.get("database"),
                schema_name=node.get("schema"), compiled_sql_redacted=redacted, compiled_sql_hash=digest,
                sql_parse_status=status, depends_on_unique_ids=tuple((node.get("depends_on") or {}).get("nodes") or ()),
                column_names=tuple(sorted((node.get("columns") or {}).keys())))
    return out


def manifest_lineage(manifest: dict[str, Any], dialect: str = "postgres", *,
                     catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """Table edges (declared dependencies known to the manifest) and column edges for every resource.
    `catalog` is dbt's catalog.json (its node/source columns resolve unqualified references)."""
    resources = resources_from_manifest(manifest)
    columns: dict[str, list[str]] = {}
    for section in ("nodes", "sources"):
        for uid, entry in ((catalog or {}).get(section) or {}).items():
            columns[uid] = sorted((entry.get("columns") or {}).keys(), key=str.lower)
    table_edges, column_edges = [], []
    for uid, res in sorted(resources.items()):
        for dep in res.depends_on_unique_ids:
            if dep in resources:
                table_edges.append({"source_unique_id": dep, "target_unique_id": uid})
        deps = [resources[d] for d in res.depends_on_unique_ids if d in resources]
        column_edges += [_as_dict(e) for e in extract_column_lineage(res, deps, dialect, catalog_columns=columns or None)]
    return {"resources": {u: r.sql_parse_status for u, r in sorted(resources.items())}, "table_edges": table_edges,
            "column_edges": column_edges}


def _as_dict(edge: ColumnLineageEdge) -> dict[str, Any]:
    return {f: getattr(edge, f) for f in edge.__slots__}
