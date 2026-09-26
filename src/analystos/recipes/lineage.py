"""Executed column lineage from the recipe IR (ADR-0023 decision 5, P6-07).

Lineage comes from the IR, node by node, not from re-parsing generated SQL: every output column is
traced to the source columns it is computed from, with OpenLineage transformation types (`DIRECT`:
IDENTITY / TRANSFORMATION / AGGREGATION; `INDIRECT`: FILTER / JOIN / GROUP_BY / SORT / WINDOW for
columns that decide which rows exist). The result becomes a `columnLineage` dataset facet
(OpenLineage ColumnLineageDatasetFacet 1-2-0, pinned in `evidence/schema`) on the output of the
recipe run's events; the SQL facet carries the compiled SQL with its literals redacted.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlglot import exp

from analystos.contracts.recipe import (
    AggregateNode,
    CastNode,
    DedupeNode,
    DeriveNode,
    FilterNode,
    JoinNode,
    OutputNode,
    RenameNode,
    SelectNode,
    SourceNode,
    UnionNode,
    ValidatedRecipe,
    WindowNode,
)
from analystos.evidence.openlineage import facet, source_namespace
from analystos.evidence.schemas import OL_RUN_EVENT

PRODUCER = "https://github.com/context2ai/analystos/recipes"
_RANK = {"IDENTITY": 0, "TRANSFORMATION": 1, "AGGREGATION": 2}

Ref = tuple[str, str]  # (asset, column)


def _merge(into: dict[Ref, str], ref: Ref, subtype: str) -> None:
    if ref not in into or _RANK[subtype] > _RANK[into[ref]]:
        into[ref] = subtype


class _Tracer:
    def __init__(self, v: ValidatedRecipe) -> None:
        self.v = v
        self.memo: dict[tuple[str, str], dict[Ref, str]] = {}

    def direct(self, node_id: str, column: str) -> dict[Ref, str]:
        """Source columns an output column is computed from, with the strongest transformation on the way."""
        key = (node_id, column)
        if key in self.memo:
            return self.memo[key]
        node = self.v.nodes[node_id]
        out: dict[Ref, str] = {}

        def up(nid: str, col: str, subtype: str = "IDENTITY") -> None:
            for ref, st in self.direct(nid, col).items():
                _merge(out, ref, st if _RANK[st] >= _RANK[subtype] else subtype)

        if isinstance(node, SourceNode):
            out[(node.asset, column)] = "IDENTITY"
        elif isinstance(node, (SelectNode, FilterNode, DedupeNode, OutputNode)):
            up(node.inputs()[0], column)
        elif isinstance(node, CastNode):
            up(node.input, column, "TRANSFORMATION" if column in node.casts else "IDENTITY")
        elif isinstance(node, DeriveNode):
            tree = self.v.exprs.get((node.id, column))
            if tree is None:
                up(node.input, column)
            else:
                bare = isinstance(tree, exp.Column)
                for name in sorted({c.name for c in tree.find_all(exp.Column)}):
                    up(node.input, name, "IDENTITY" if bare else "TRANSFORMATION")
        elif isinstance(node, RenameNode):
            back = {new: old for old, new in node.mapping.items()}
            up(node.input, back.get(column, column))
        elif isinstance(node, JoinNode):
            if column in self.v.columns(node.left):
                up(node.left, column)
            else:
                up(node.right, column)
        elif isinstance(node, AggregateNode):
            measure = next((m for m in node.measures if m.name == column), None)
            if measure is None:
                up(node.input, column)
            elif measure.column:
                up(node.input, measure.column, "AGGREGATION")
        elif isinstance(node, WindowNode):
            wcol = next((w for w in node.columns if w.name == column), None)
            if wcol is None:
                up(node.input, column)
            elif wcol.column:
                up(node.input, wcol.column, "IDENTITY" if wcol.func in ("lag", "lead") else "AGGREGATION")
        elif isinstance(node, UnionNode):
            for i in node.union_inputs:
                up(i, column)
        self.memo[key] = out
        return out

    def indirect(self, node_id: str) -> dict[Ref, set[str]]:
        """Source columns that decide which rows reach `node_id` (filters, join keys, grouping, ordering)."""
        out: dict[Ref, set[str]] = {}
        for nid in self.v.ancestors(node_id):
            node = self.v.nodes[nid]
            uses: list[tuple[str, str, str]] = []  # (input node, column, subtype)
            if isinstance(node, FilterNode):
                tree = self.v.exprs[(node.id, "predicate")]
                uses += [(node.input, c.name, "FILTER") for c in tree.find_all(exp.Column)]
            elif isinstance(node, JoinNode):
                uses += [(node.left, k.left, "JOIN") for k in node.on] + [(node.right, k.right, "JOIN") for k in node.on]
            elif isinstance(node, AggregateNode):
                uses += [(node.input, k, "GROUP_BY") for k in node.keys]
            elif isinstance(node, DedupeNode):
                uses += [(node.input, k, "GROUP_BY") for k in node.keys]
                uses += [(node.input, o.column, "SORT") for o in node.order]
            elif isinstance(node, WindowNode):
                for w in node.columns:
                    uses += [(node.input, p, "WINDOW") for p in w.partition_by]
                    uses += [(node.input, o.column, "WINDOW") for o in w.order_by]
            for inp, col, subtype in uses:
                for ref in self.direct(inp, col):
                    out.setdefault(ref, set()).add(subtype)
        return out


def column_lineage(v: ValidatedRecipe) -> list[dict[str, Any]]:
    """Per output: {output, columns: {column: [{asset, column, subtype}]}, dataset: [{asset, column, subtypes}]}."""
    tracer = _Tracer(v)
    out = []
    for o in v.outputs():
        cols = {}
        for c in o.output_schema or []:
            cols[c.name] = [{"asset": a, "column": col, "subtype": st}
                            for (a, col), st in sorted(tracer.direct(o.id, c.name).items())]
        dataset = [{"asset": a, "column": col, "subtypes": sorted(sts)} for (a, col), sts in sorted(tracer.indirect(o.id).items())]
        out.append({"output": o.name, "columns": cols, "dataset": dataset})
    return out


def column_lineage_facet(lineage: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    """An output's lineage as an OpenLineage ColumnLineageDatasetFacet."""
    def field(asset: str, column: str, ttype: str, subtype: str) -> dict[str, Any]:
        return {"namespace": source_namespace(sources.get(asset)), "name": asset, "field": column,
                "transformations": [{"type": ttype, "subtype": subtype}]}

    fields = {name: {"inputFields": [field(r["asset"], r["column"], "DIRECT", r["subtype"]) for r in refs]}
              for name, refs in lineage["columns"].items()}
    dataset = [field(r["asset"], r["column"], "INDIRECT", st) for r in lineage["dataset"] for st in r["subtypes"]]
    return facet("ColumnLineageDatasetFacet", fields=fields, dataset=dataset)


def openlineage_events(*, workspace_id: str, recipe_name: str, recipe_version: int, recipe_run_id: str,
                       lineage: list[dict[str, Any]], sources: dict[str, str], outputs: dict[str, dict[str, Any]],
                       sql: dict[str, str], dialect: str, started_at: datetime, finished_at: datetime,
                       status: str, error: str | None = None) -> list[dict[str, Any]]:
    """START + COMPLETE/FAIL per output of a recipe run. `outputs` = {output name: {namespace, name,
    columns}} of the materialized (or previewed) relation; `sql` = redacted compiled SQL per output."""
    events: list[dict[str, Any]] = []
    ns = f"analystos:{workspace_id}"
    for entry in lineage:
        name = entry["output"]
        target = outputs.get(name) or {}
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"analystos:recipe_run:{recipe_run_id}:{name}"))
        job: dict[str, Any] = {"namespace": ns, "name": f"recipe.{recipe_name}.v{recipe_version}.{name}",
                               "facets": {"jobType": facet("JobTypeJobFacet", processingType="BATCH",
                                                           integration="ANALYSTOS", jobType="RECIPE")}}
        if sql.get(name):
            job["facets"]["sql"] = facet("SQLJobFacet", query=sql[name], dialect=dialect)
        assets = sorted({r["asset"] for refs in entry["columns"].values() for r in refs} |
                        {r["asset"] for r in entry["dataset"]})
        inputs = [{"namespace": source_namespace(sources.get(a)), "name": a, "facets": {}} for a in assets]
        output = {"namespace": target.get("namespace") or f"analystos://recipe/{recipe_name}",
                  "name": target.get("name") or name,
                  "facets": {"schema": facet("SchemaDatasetFacet", fields=[{"name": c["name"], "type": c["type"]}
                                                                           for c in target.get("columns") or []]),
                             "columnLineage": column_lineage_facet(entry, sources)}}
        ok = status == "succeeded"
        terminal: dict[str, Any] = {}
        if not ok and error:
            terminal["errorMessage"] = facet("ErrorMessageRunFacet", message=error[:2000], programmingLanguage="SQL")
        for event_type, when, facets_ in (("START", started_at, {}), ("COMPLETE" if ok else "FAIL", finished_at, terminal)):
            events.append({"eventType": event_type, "eventTime": when.isoformat(), "producer": PRODUCER,
                           "schemaURL": OL_RUN_EVENT, "run": {"runId": run_id, "facets": facets_}, "job": job,
                           "inputs": inputs, "outputs": [output] if event_type != "START" else []})
    return events
