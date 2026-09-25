"""Bundle validation (shared by every publisher) and the no-BI-tool ``preview`` destination."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle, PublishResult
from analystos.core.errors import InvalidInput
from analystos.publishing.base import ExternalId, PublishContext, bundle_hash, workspace_slug

_NEEDS_METRIC = {"kpi", "line", "bar", "stacked_bar", "heatmap", "pie", "treemap", "scatter"}
_NEEDS_DIMENSION = {"pie", "treemap"}
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def chart_metric_names(chart: ChartSpec) -> list[str]:
    """Primary metric first, de-duplicated, preserving order."""
    out: list[str] = []
    for name in ([chart.metric] if chart.metric else []) + list(chart.metrics):
        if name and name not in out:
            out.append(name)
    return out


def _dup(values: list[str]) -> list[str]:
    return sorted(v for v, n in Counter(values).items() if n > 1)


def validate_bundle(bundle: PublishBundle) -> list[str]:
    """Structural validation before any external call. Returns human-readable errors (empty = valid)."""
    errors: list[str] = []
    datasets: dict[str, DatasetDef] = {d.name: d for d in bundle.datasets}
    metrics: dict[str, MetricDef] = {m.name: m for m in bundle.metrics}
    charts: dict[str, ChartSpec] = {c.key: c for c in bundle.charts}

    if not bundle.workspace_id.strip():
        errors.append("workspace_id is empty")
    if not bundle.dashboards:
        errors.append("bundle has no dashboards")
    for label, names in (
        ("dataset name", [d.name for d in bundle.datasets]),
        ("metric name", [m.name for m in bundle.metrics]),
        ("chart key", [c.key for c in bundle.charts]),
        ("dashboard key", [d.key for d in bundle.dashboards]),
        ("dashboard title", [d.title for d in bundle.dashboards]),
    ):
        for dup in _dup(names):
            errors.append(f"duplicate {label} {dup!r}")
    for label, names in (
        ("dataset name", list(datasets)),
        ("chart key", list(charts)),
        ("dashboard key", [d.key for d in bundle.dashboards]),
    ):
        for n in names:
            if not _NAME_RE.match(n):
                errors.append(f"{label} {n!r} must match [A-Za-z0-9_.-]+")

    for d in bundle.datasets:
        dcols = {c.get("name") for c in d.columns}
        if not d.sql.strip():
            errors.append(f"dataset {d.name!r} has empty sql")
        if d.time_column and dcols and d.time_column not in dcols:
            errors.append(f"dataset {d.name!r} time_column {d.time_column!r} is not one of its columns")
    for m in bundle.metrics:
        if not m.sql_expression.strip():
            errors.append(f"metric {m.name!r} has empty sql_expression")

    for c in bundle.charts:
        where = f"chart {c.key!r}"
        ds = datasets.get(c.dataset)
        if ds is None:
            errors.append(f"{where} references unknown dataset {c.dataset!r}")
        mnames = chart_metric_names(c)
        for mn in mnames:
            if mn not in metrics:
                errors.append(f"{where} references unknown metric {mn!r}")
        if c.chart_type in _NEEDS_METRIC and not mnames:
            errors.append(f"{where} ({c.chart_type}) needs a metric")
        if c.chart_type in _NEEDS_DIMENSION and not c.dimension:
            errors.append(f"{where} ({c.chart_type}) needs a dimension")
        if c.chart_type == "heatmap" and not (c.dimension and (c.series or (ds and ds.time_column))):
            errors.append(f"{where} (heatmap) needs a dimension and a series (or a dataset time_column)")
        if c.chart_type == "histogram" and not (c.dimension or _histogram_column(c, metrics)):
            errors.append(f"{where} (histogram) needs a numeric dimension column")
        if ds is not None:
            if c.chart_type == "line" and not ds.time_column:
                errors.append(f"{where} (line) needs dataset {ds.name!r} to declare a time_column")
            if c.chart_type in {"bar", "stacked_bar"} and not (c.dimension or ds.time_column):
                errors.append(f"{where} ({c.chart_type}) needs a dimension or a dataset time_column")
            if c.chart_type == "scatter" and not (c.dimension or ds.time_column):
                errors.append(f"{where} (scatter) needs a dimension or a dataset time_column")
            if c.chart_type == "table" and not (mnames or c.dimension or ds.columns):
                errors.append(f"{where} (table) needs metrics, a dimension, or known dataset columns")
            cols = {col.get("name") for col in ds.columns}
            if cols:
                for attr in ("dimension", "series"):
                    val = getattr(c, attr)
                    if val and val not in cols:
                        errors.append(f"{where} {attr} {val!r} is not a column of dataset {ds.name!r}")

    for dash in bundle.dashboards:
        errors.extend(_validate_dashboard(dash, charts, datasets))
    return errors


def _histogram_column(chart: ChartSpec, metrics: dict[str, MetricDef]) -> str | None:
    if chart.dimension:
        return chart.dimension
    for mn in chart_metric_names(chart):
        m = metrics.get(mn)
        if m and m.source_columns:
            return m.source_columns[0]
    return None


def _validate_dashboard(d: DashboardSpec, charts: dict[str, ChartSpec], datasets: dict[str, DatasetDef]) -> list[str]:
    errors: list[str] = []
    where = f"dashboard {d.key!r}"
    if not d.charts:
        errors.append(f"{where} has no charts")
    for dup in _dup(d.charts):
        errors.append(f"{where} lists chart {dup!r} more than once")
    for key in d.charts:
        if key not in charts:
            errors.append(f"{where} references unknown chart {key!r}")
    if d.layout:
        placed = [str(cell.get("chart")) for cell in d.layout]
        for dup in _dup(placed):
            errors.append(f"{where} layout places chart {dup!r} more than once")
        for key in placed:
            if key not in d.charts:
                errors.append(f"{where} layout places chart {key!r} that the dashboard does not list")
        for key in d.charts:
            if key not in placed:
                errors.append(f"{where} layout does not place chart {key!r}")
        for cell in d.layout:
            w = cell.get("width")
            if w is not None and not (isinstance(w, int) and 1 <= w <= 12):
                errors.append(f"{where} layout width {w!r} for {cell.get('chart')!r} must be an int in 1..12")
    used = [datasets[charts[k].dataset] for k in d.charts if k in charts and charts[k].dataset in datasets]
    known_cols = {col.get("name") for ds in used for col in ds.columns}
    if known_cols:
        for f in d.native_filters:
            if f not in known_cols:
                errors.append(f"{where} native filter {f!r} is not a column of any dataset it uses")
    return errors


def ensure_valid(bundle: PublishBundle) -> None:
    errors = validate_bundle(bundle)
    if errors:
        raise InvalidInput("publish bundle is invalid", details={"errors": errors})


class PreviewPublisher:
    """Destination ``preview``: validates and assigns deterministic local ids. No external calls.

    Used when no BI tool is configured (the UI renders ChartSpec.preview itself) and in tests.
    """

    destination = "preview"

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}

    def _id(self, kind: str, ws: str, name: str) -> str:
        return f"preview:{workspace_slug(ws)}:{kind}:{name}"

    def _put(self, oid: str, payload: dict[str, Any]) -> str:
        self.objects[oid] = payload
        return oid

    def test_connection(self) -> dict[str, Any]:
        return {"ok": True, "destination": self.destination, "url": None, "error": None}

    def create_dataset(self, dataset: DatasetDef, ctx: PublishContext) -> ExternalId:
        return self._put(self._id("dataset", ctx.workspace_id, dataset.name), dataset.model_dump())

    def update_dataset(self, external_id: ExternalId, dataset: DatasetDef, ctx: PublishContext) -> ExternalId:
        return self._put(str(external_id), dataset.model_dump())

    def create_metric(self, dataset_external_id: ExternalId, metric: MetricDef, ctx: PublishContext) -> ExternalId:
        return self._put(f"{dataset_external_id}:metric:{metric.name}", metric.model_dump())

    def create_chart(self, chart: ChartSpec, ctx: PublishContext) -> ExternalId:
        return self._put(self._id("chart", ctx.workspace_id, chart.key), chart.model_dump())

    def update_chart(self, external_id: ExternalId, chart: ChartSpec, ctx: PublishContext) -> ExternalId:
        return self._put(str(external_id), chart.model_dump())

    def create_dashboard(self, dashboard: DashboardSpec, ctx: PublishContext) -> ExternalId:
        return self._put(self._id("dashboard", ctx.workspace_id, dashboard.key), dashboard.model_dump())

    def update_dashboard(self, external_id: ExternalId, dashboard: DashboardSpec, ctx: PublishContext) -> ExternalId:
        return self._put(str(external_id), dashboard.model_dump())

    def publish_dashboard(self, external_id: ExternalId) -> str:
        if str(external_id) in self.objects:
            self.objects[str(external_id)]["published"] = True
        return f"preview://{external_id}"

    def create_report(self, dashboard_external_id: ExternalId, *, recipients: list[str], name: str = "") -> ExternalId:
        raise NotImplementedError("scheduled reports need a BI destination; the preview destination has none")

    def schedule_report(self, report_external_id: ExternalId, *, cron: str, timezone: str = "UTC") -> None:
        raise NotImplementedError("report scheduling is Phase 3")

    def create_alert(self, chart_external_id: ExternalId, *, condition: dict[str, Any], recipients: list[str]) -> ExternalId:
        raise NotImplementedError("alerts are Phase 3")

    def export_artifact(self, object_type: str, external_id: ExternalId) -> bytes:
        import json

        obj = self.objects.get(str(external_id))
        if obj is None:
            raise InvalidInput(f"unknown preview object {external_id!r}")
        return json.dumps({"type": object_type, "id": str(external_id), "object": obj}, default=str).encode()

    def publish(self, bundle: PublishBundle, *, idempotency_key: str, previous: dict | None = None) -> PublishResult:
        errors = validate_bundle(bundle)
        if errors:
            return PublishResult(destination=self.destination, status="failed", errors=errors)
        ctx = PublishContext.for_bundle(bundle, idempotency_key=idempotency_key, previous=previous)
        ids = ctx.external_ids
        ids["database"] = f"preview:{workspace_slug(bundle.workspace_id)}:database"
        for ds in bundle.datasets:
            ids["datasets"][ds.name] = self.create_dataset(ds, ctx)
        for c in bundle.charts:
            ds_id = ids["datasets"][c.dataset]
            for mn in chart_metric_names(c):
                ids["metrics"][f"{c.dataset}:{mn}"] = self.create_metric(ds_id, ctx.metrics[mn], ctx)
            ids["charts"][c.key] = self.create_chart(c, ctx)
        urls: dict[str, str] = {}
        for d in bundle.dashboards:
            ids["dashboards"][d.key] = self.create_dashboard(d, ctx)
            urls[d.key] = self.publish_dashboard(ids["dashboards"][d.key])
        return PublishResult(destination=self.destination, status="succeeded", external_ids=ids, urls=urls)

    def rollback(self, external_ids: dict[str, Any]) -> list[str]:
        deleted: list[str] = []
        for kind in ("dashboards", "charts", "datasets"):
            for oid in (external_ids.get(kind) or {}).values():
                if self.objects.pop(str(oid), None) is not None:
                    deleted.append(str(oid))
        return deleted


__all__ = ["PreviewPublisher", "validate_bundle", "ensure_valid", "chart_metric_names", "bundle_hash"]
