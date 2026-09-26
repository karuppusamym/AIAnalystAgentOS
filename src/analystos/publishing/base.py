"""Platform-independent BI publishing interface (§32-§35).

A publisher turns an approved :class:`PublishBundle` into objects in a BI tool. The high-level
``publish`` must be idempotent (reconcile-before-create): replaying the same bundle after a crash
converges on the same external objects instead of duplicating them. ``external_ids`` returned in
:class:`PublishResult` is the reconcile/rollback handle the caller persists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from analystos.contracts.bi import ChartSpec, DashboardSpec, DatasetDef, MetricDef, PublishBundle, PublishResult
from analystos.core.errors import InvalidInput
from analystos.core.ids import stable_hash

if TYPE_CHECKING:
    from analystos.core.config import Settings

ExternalId = int | str


def bundle_hash(bundle: PublishBundle) -> str:
    """The hash an approval binds to; stamped on published objects for drift detection."""
    return stable_hash(bundle.model_dump(mode="json"))


def workspace_slug(workspace_id: str) -> str:
    """Lower-case, [a-z0-9_] only. Used in every external name so reconcile can scope by workspace."""
    return re.sub(r"[^a-z0-9]+", "_", workspace_id.lower()).strip("_") or "ws"


@dataclass
class PublishContext:
    """State threaded through one publish: what we are publishing and what already exists."""

    workspace_id: str
    bundle: PublishBundle
    idempotency_key: str
    bundle_hash: str
    previous: dict[str, Any] = field(default_factory=dict)
    external_ids: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_bundle(cls, bundle: PublishBundle, *, idempotency_key: str, previous: dict | None = None) -> PublishContext:
        return cls(
            workspace_id=bundle.workspace_id,
            bundle=bundle,
            idempotency_key=idempotency_key,
            bundle_hash=bundle_hash(bundle),
            previous=dict(previous or {}),
            external_ids={
                "database": None,
                "database_created": False,
                "datasets": {},
                "metrics": {},
                "charts": {},
                "dashboards": {},
                "bundle_hash": bundle_hash(bundle),
                "idempotency_key": idempotency_key,
            },
        )

    def dataset(self, name: str) -> DatasetDef:
        for ds in self.bundle.datasets:
            if ds.name == name:
                return ds
        raise InvalidInput(f"unknown dataset {name!r}")

    @property
    def metrics(self) -> dict[str, MetricDef]:
        return {m.name: m for m in self.bundle.metrics}

    def chart(self, key: str) -> ChartSpec:
        for c in self.bundle.charts:
            if c.key == key:
                return c
        raise InvalidInput(f"unknown chart {key!r}")


@runtime_checkable
class BIPublisher(Protocol):
    """What every BI adapter (Superset, Power BI, preview) implements."""

    destination: str

    def test_connection(self) -> dict[str, Any]:
        """Never raises; returns {"ok": bool, "destination": ..., "url": ..., "error": ...}."""
        ...

    # -- building blocks ---------------------------------------------------------------------
    def create_dataset(self, dataset: DatasetDef, ctx: PublishContext) -> ExternalId: ...

    def update_dataset(self, external_id: ExternalId, dataset: DatasetDef, ctx: PublishContext) -> ExternalId: ...

    def create_metric(self, dataset_external_id: ExternalId, metric: MetricDef, ctx: PublishContext) -> ExternalId: ...

    def create_chart(self, chart: ChartSpec, ctx: PublishContext) -> ExternalId: ...

    def update_chart(self, external_id: ExternalId, chart: ChartSpec, ctx: PublishContext) -> ExternalId: ...

    def create_dashboard(self, dashboard: DashboardSpec, ctx: PublishContext) -> ExternalId: ...

    def update_dashboard(self, external_id: ExternalId, dashboard: DashboardSpec, ctx: PublishContext) -> ExternalId: ...

    def publish_dashboard(self, external_id: ExternalId) -> str:
        """Make the dashboard visible to its audience; returns its URL."""
        ...

    def create_report(self, dashboard_external_id: ExternalId, *, recipients: list[str], name: str = "") -> ExternalId: ...

    def schedule_report(self, report_external_id: ExternalId, *, cron: str, timezone: str = "UTC") -> None: ...

    def create_alert(self, chart_external_id: ExternalId, *, condition: dict[str, Any], recipients: list[str]) -> ExternalId: ...

    def export_artifact(self, object_type: str, external_id: ExternalId) -> bytes: ...

    # -- high level ----------------------------------------------------------------------------
    def publish(self, bundle: PublishBundle, *, idempotency_key: str, previous: dict | None = None) -> PublishResult: ...

    def rollback(self, external_ids: dict[str, Any]) -> list[str]: ...


def default_destination(allowed: list[str], settings: Settings | None = None) -> str | None:
    """The first allowed destination this installation can reach; `preview` (in-platform, no external
    side effect) when Superset is not installed (no `bi` profile, ADR-0025) or nothing else is allowed."""
    if settings is None:
        from analystos.core.config import get_settings

        settings = get_settings()
    usable = [d for d in allowed if d != "superset" or settings.superset_url]
    return usable[0] if usable else "preview"


def get_publisher(destination: str, settings: Settings | None = None) -> BIPublisher:
    """Factory. ``preview`` needs no BI tool; ``superset`` uses Settings.superset_*."""
    if destination == "preview":
        from analystos.publishing.preview import PreviewPublisher

        return PreviewPublisher()
    if destination == "superset":
        from analystos.core.config import get_settings
        from analystos.publishing.superset import SupersetPublisher

        return SupersetPublisher(settings or get_settings())
    if destination == "powerbi":
        raise InvalidInput("Power BI publishing is not implemented yet (Phase 3); use 'superset' or 'preview'")
    raise InvalidInput(f"unknown BI destination {destination!r}")
