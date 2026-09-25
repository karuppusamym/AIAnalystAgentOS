"""PostgreSQL connector (pushdown by default): the generic SQL connector with Postgres defaults.

The gateway queries the source in place through ``sqlalchemy_url()``, which must name a
least-privilege, read-only identity; the gateway additionally wraps every query in a READ ONLY
transaction with a statement timeout, and the connector's own sessions (discovery, and extraction
when a Postgres source is explicitly staged) run with ``SET SESSION CHARACTERISTICS AS TRANSACTION
READ ONLY``. Discovery uses the SQLAlchemy Inspector (tables, views, materialized views, keys,
comments) plus ``pg_class.reltuples`` row estimates; include/exclude and max_tables apply.
"""
from __future__ import annotations

from typing import Any

from analystos.connectors.generic_sql import GenericSQLConnector

DEFAULT_SCHEMAS = ["public"]


class PostgresConnector(GenericSQLConnector):
    def __init__(self, config: dict[str, Any], secret_ref: str | None = None, *, password: str | None = None,
                 **kwargs: Any) -> None:
        config = dict(config or {})
        if not config.get("schemas"):
            config["schemas"] = list(DEFAULT_SCHEMAS)  # unchanged default for existing sources
        super().__init__("postgres", config, secret_ref, password=password, **kwargs)
        self.host = str(config.get("host") or "")
        self.port = int(config.get("port") or 5432)
        self.database = str(config.get("database") or "")
        self.username = str(config.get("username") or "")
        self.sslmode = config.get("sslmode")
