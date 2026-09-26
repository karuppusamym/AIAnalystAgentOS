"""Superset metadata as OKF documents (P4-K06; feeds existing-dashboard mode, BI-011).

Read-only: only GETs through the existing `SupersetClient` (datasets, charts, dashboards and the
charts of each dashboard). Three facets, each allowed to fail on its own (a 403 on charts costs the
charts, not the datasets).

Tenancy: a Superset instance can hold objects AnalystOS published for other workspaces. Those are
recognised by their deterministic names (`aos_<ws>_` datasets and charts, `aos-<ws>-` dashboard slugs,
the `AnalystOS Analytics (<ws>)` database) and never read into this workspace. `include` globs can
narrow further.

Value-free: chart `params`/`form_data` (adhoc filters carry literal values) and virtual-dataset SQL are
not copied; a dataset is described by its columns and metrics, a chart by its type and dataset.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
from typing import Any

from analystos.knowledge import okf
from analystos.knowledge.crawl_docs import cell

SUPERSET_ACTOR = "process:analystos-superset-crawler"
MAX_OBJECTS = 500
_AOS_DB = re.compile(r"^AnalystOS Analytics \((.+)\)$")


class Scope:
    """Which Superset objects belong to (or may be read by) one workspace."""

    def __init__(self, workspace_id: str, include: list[str] | None = None) -> None:
        from analystos.publishing.superset import SupersetPublisher

        self.workspace_id = workspace_id
        self.own_prefix = SupersetPublisher.chart_prefix(workspace_id).lower()  # aos_<ws>_ (datasets and charts)
        self.own_slug = SupersetPublisher.dashboard_slug(workspace_id, "")  # aos-<ws>-
        self.include = [p.lower() for p in include or []]

    def _included(self, name: str) -> bool:
        return not self.include or any(fnmatch.fnmatch(name.lower(), p) for p in self.include)

    def _foreign_name(self, name: str) -> bool:
        n = name.lower()
        return n.startswith("aos_") and not n.startswith(self.own_prefix)

    def dataset(self, d: dict[str, Any]) -> bool:
        db = str((d.get("database") or {}).get("database_name") or "")
        m = _AOS_DB.match(db)
        if m and m.group(1) != self.workspace_id:
            return False
        name = str(d.get("table_name") or "")
        return not self._foreign_name(name) and self._included(name)

    def chart(self, c: dict[str, Any]) -> bool:
        name = str(c.get("slice_name") or "")
        return not self._foreign_name(name) and self._included(name)

    def dashboard(self, d: dict[str, Any]) -> bool:
        slug = str(d.get("slug") or "").lower()
        if slug.startswith("aos-") and not slug.startswith(self.own_slug):
            return False
        return self._included(str(d.get("dashboard_title") or slug))


def dataset_path(i: Any) -> str:
    return f"bi/superset/datasets/{int(i)}.md"


def chart_path(i: Any) -> str:
    return f"bi/superset/charts/{int(i)}.md"


def dashboard_path(i: Any) -> str:
    return f"bi/superset/dashboards/{int(i)}.md"


def _rel(from_path: str, to_path: str) -> str:
    return posixpath.relpath(to_path, posixpath.dirname(from_path))


def _fm(kind: str, type_: str, title: str, oid: Any, url: str | None, description: str | None, tags: list[str],
        extra: dict[str, Any]) -> dict[str, Any]:
    fm: dict[str, Any] = {"type": type_, "title": title, "status": "draft", "tags": sorted({"superset", "bi", *tags}),
                          "generated": {"by": SUPERSET_ACTOR}, "resource": url or f"superset://{kind}/{oid}",
                          "analystos": {"kind": f"superset_{kind}", "origin": "superset", "trusted": False,
                                        "superset_id": oid, **extra}}
    if description:
        from analystos.knowledge.entries import first_sentence

        desc = first_sentence(description)
        if desc:
            fm["description"] = desc
    return fm


def dataset_document(d: dict[str, Any], base_url: str = "") -> tuple[str, str]:
    path = dataset_path(d["id"])
    name = str(d.get("table_name") or d.get("name") or d["id"])
    cols = d.get("columns") or []
    metrics = d.get("metrics") or []
    fq = f"{d['schema']}.{name}" if d.get("schema") else name
    fm = _fm("dataset", "BI Dataset", name, d["id"], f"{base_url}/explore/?datasource_type=table&datasource_id={d['id']}"
             if base_url else None, d.get("description"), ["dataset"],
             {"database": (d.get("database") or {}).get("database_name"), "schema": d.get("schema"),
              "dataset_kind": d.get("kind") or ("virtual" if d.get("sql") else "physical"),
              "mapped_columns": [f"{fq}.{c.get('column_name')}" for c in cols][:100],
              "synonyms": [m.get("verbose_name") for m in metrics if m.get("verbose_name")][:50]})
    body = ["# Dataset", "", f"Superset dataset `{name}` ({fm['analystos']['dataset_kind']}) in database "
            f"{cell((d.get('database') or {}).get('database_name'))}, schema {cell(d.get('schema'))}.", ""]
    if d.get("description"):
        body += [str(d["description"]).strip(), ""]
    body += ["# Columns", "", "| column | type | time | label | description |", "|---|---|---|---|---|"]
    body += [f"| {cell(c.get('column_name'))} | {cell(c.get('type'))} | {bool(c.get('is_dttm'))} | {cell(c.get('verbose_name'))} | "
             f"{cell(c.get('description'))} |" for c in cols] or ["| - | - | - | - | - |"]
    body += ["", "# Metrics", "", "| metric | label | expression | description |", "|---|---|---|---|"]
    body += [f"| {cell(m.get('metric_name'))} | {cell(m.get('verbose_name'))} | `{cell(m.get('expression'))}` | "
             f"{cell(m.get('description'))} |" for m in metrics] or ["| - | - | - | - |"]
    return path, okf.render_document(fm, "\n".join(body))


def chart_document(c: dict[str, Any], *, datasets: set[int], dashboards: dict[int, str], base_url: str = "") -> tuple[str, str]:
    path = chart_path(c["id"])
    name = str(c.get("slice_name") or c["id"])
    ds_id = c.get("datasource_id")
    fm = _fm("chart", "Chart", name, c["id"], f"{base_url}{c['url']}" if base_url and c.get("url") else None,
             c.get("description"), ["chart", re.sub(r"[^a-z0-9]+", "-", str(c.get("viz_type") or "chart").lower())],
             {"viz_type": c.get("viz_type"), "dataset_id": ds_id})
    ds_name = (c.get("table") or {}).get("table_name") or c.get("datasource_name_text") or ds_id
    ds_ref = f"[{ds_name}]({_rel(path, dataset_path(ds_id))})" if ds_id in datasets else cell(ds_name)
    body = ["# Chart", "", f"Superset chart `{cell(name)}`, type {cell(c.get('viz_type'))}, over dataset {ds_ref}.", ""]
    if c.get("description"):
        body += [str(c["description"]).strip(), ""]
    ids = [(d.get("id") if isinstance(d, dict) else d) for d in c.get("dashboards") or []]
    body += ["# Dashboards", ""] + [f"- [{cell(dashboards[i])}]({_rel(path, dashboard_path(i))})" if i in dashboards else f"- {i}"
                                    for i in ids if i is not None]
    if not ids:
        body.append("- none")
    return path, okf.render_document(fm, "\n".join(body))


def dashboard_document(d: dict[str, Any], charts: list[dict[str, Any]], *, known_charts: set[int], base_url: str = "") -> tuple[str, str]:
    path = dashboard_path(d["id"])
    title = str(d.get("dashboard_title") or d.get("slug") or d["id"])
    fm = _fm("dashboard", "Dashboard", title, d["id"], f"{base_url}{d['url']}" if base_url and d.get("url") else None, None,
             ["dashboard"], {"slug": d.get("slug"), "published": bool(d.get("published")),
                             "chart_ids": sorted(int(c["id"]) for c in charts if c.get("id") is not None)})
    body = ["# Dashboard", "", f"Superset dashboard `{cell(title)}`" + (f" (slug `{d['slug']}`)" if d.get("slug") else "")
            + (", published." if d.get("published") else ", draft."), "", "# Charts", ""]
    body += [f"- [{cell(c.get('slice_name'))}]({_rel(path, chart_path(c['id']))})" if c.get("id") in known_charts
             else f"- {cell(c.get('slice_name'))}" for c in charts] or ["- none"]
    return path, okf.render_document(fm, "\n".join(body))


def collect(client: Any, workspace_id: str, facets: Any, *, include: list[str] | None = None, base_url: str = "",
            max_objects: int = MAX_OBJECTS) -> dict[str, str]:
    """Read datasets, charts and dashboards (GET only) and render their documents. Each kind is a
    facet on `facets` (services.facets.Facets)."""
    scope = Scope(workspace_id, include)
    docs: dict[str, str] = {}
    datasets: dict[int, dict[str, Any]] = {}
    charts: dict[int, dict[str, Any]] = {}
    dashboards: dict[int, dict[str, Any]] = {}

    def read_datasets() -> dict[str, Any]:
        for row in [r for r in client.list("dataset", []) if scope.dataset(r)][:max_objects]:
            detail = client.get(f"/api/v1/dataset/{int(row['id'])}").get("result") or row
            datasets[int(row["id"])] = {**row, **detail}
        return {"count": len(datasets)}

    def read_charts() -> dict[str, Any]:
        for row in [r for r in client.list("chart", []) if scope.chart(r)][:max_objects]:
            charts[int(row["id"])] = row
        return {"count": len(charts)}

    def read_dashboards() -> dict[str, Any]:
        for row in [r for r in client.list("dashboard", []) if scope.dashboard(r)][:max_objects]:
            members = client.get(f"/api/v1/dashboard/{int(row['id'])}/charts").get("result") or []
            dashboards[int(row["id"])] = {**row, "_charts": [m for m in members if scope.chart(m)]}
        return {"count": len(dashboards)}

    facets.run("superset.datasets", read_datasets)
    facets.run("superset.charts", read_charts)
    facets.run("superset.dashboards", read_dashboards)
    titles = {i: str(d.get("dashboard_title") or i) for i, d in dashboards.items()}
    for d in datasets.values():
        path, text = dataset_document(d, base_url)
        docs[path] = text
    for c in charts.values():
        path, text = chart_document(c, datasets=set(datasets), dashboards=titles, base_url=base_url)
        docs[path] = text
    for d in dashboards.values():
        path, text = dashboard_document(d, d["_charts"], known_charts=set(charts), base_url=base_url)
        docs[path] = text
    return docs
