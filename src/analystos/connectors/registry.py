"""Connector factory: control-plane Source row -> Connector instance."""
from __future__ import annotations

from typing import Any

from analystos.connectors.base import Connector
from analystos.core.errors import InvalidInput

KINDS = ("postgres", "sqlserver", "csv", "file", "servicenow")


def build_connector(source: Any, settings: Any | None = None, **overrides: Any) -> Connector:
    """Build the connector for ``source`` (an ``analystos.db.models.Source`` or any object with
    ``kind``, ``config`` and ``secret_ref``). ``overrides`` are passed to the connector constructor
    (e.g. ``http_client`` for tests, ``allowed_dir`` for file sources)."""
    kind = (getattr(source, "kind", None) or "").lower()
    config = dict(getattr(source, "config", None) or {})
    secret_ref = getattr(source, "secret_ref", None)
    if kind == "postgres":
        from analystos.connectors.postgres import PostgresConnector

        return PostgresConnector(config, secret_ref, **overrides)
    if kind == "sqlserver":
        from analystos.connectors.sqlserver import SQLServerConnector

        return SQLServerConnector(config, secret_ref, **overrides)
    if kind in ("csv", "file"):
        from analystos.connectors.csv_file import CSVFileConnector, default_upload_dir

        overrides.setdefault("allowed_dir", default_upload_dir(settings))
        return CSVFileConnector(config, secret_ref, **overrides)
    if kind == "servicenow":
        from analystos.connectors.servicenow import ServiceNowConnector

        if not config.get("instance_url") and settings is not None:
            config["instance_url"] = getattr(settings, "servicenow_mock_url", None)
        return ServiceNowConnector(config, secret_ref, **overrides)
    raise InvalidInput(f"Unknown source kind {kind!r}; expected one of {', '.join(KINDS)}")
