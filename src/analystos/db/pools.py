"""Connection-pool settings per database plane (P4-S05).

Every worker process, API process and compute-pool child keeps its own pools, so pool sizes decide
how many Postgres connections a deployment needs. They are settings, not constants:

    plane      engine                                      env prefix
    control    db/base.py (application state)              ANALYSTOS_DB_*
    analytics  gateway/engines.py for the reader identity   ANALYSTOS_ANALYTICS_*
               and pushdown sources (one pool per URL)
    loader     gateway/engines.py for the staging loader    ANALYSTOS_LOADER_*

with ``*_POOL_MODE`` (``queue`` | ``none``; analytics and loader inherit the control mode when
unset), ``*_POOL_SIZE``, ``*_MAX_OVERFLOW`` and ``*_POOL_TIMEOUT``; ``ANALYSTOS_DB_POOL_RECYCLE``
applies to all three. ``none`` opens a connection per checkout (SQLAlchemy ``NullPool``), for use
behind an external pooler that already bounds server connections.

``ANALYSTOS_DB_TRANSACTION_POOLER=true`` declares that these URLs reach Postgres through a
transaction-mode pooler (PgBouncer ``pool_mode = transaction``): psycopg's automatic server-side
prepared statements are turned off, since consecutive transactions of one client may land on
different server connections. The platform keeps no session state on these planes — role switches
and timeouts are ``SET LOCAL`` inside the query's own transaction and the scheduler's advisory lock is
transaction-scoped. The builder identity is not pooled here: it opens one direct psycopg connection
per provisioning step, and dbt sets its role for the whole session, so ``ANALYSTOS_ANALYTICS_BUILDER_URL``
must point at Postgres directly or at a session-mode pool (docs/30-runbooks/02-operations.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

Plane = Literal["control", "analytics", "loader"]
PLANES: tuple[Plane, ...] = ("control", "analytics", "loader")
_PREFIX = {"control": "db", "analytics": "analytics", "loader": "loader"}


@dataclass(frozen=True)
class PoolConfig:
    plane: str
    mode: str  # queue | none
    size: int
    max_overflow: int
    timeout: float
    recycle: int
    transaction_pooler: bool

    @property
    def max_connections(self) -> int | None:
        """Connections one process may hold on one URL of this plane (None: unbounded, NullPool)."""
        return None if self.mode == "none" else self.size + self.max_overflow


def pool_config(plane: Plane, settings: Any = None) -> PoolConfig:
    if plane not in PLANES:
        raise ValueError(f"unknown database plane {plane!r}")
    if settings is None:
        from analystos.core.config import get_settings

        settings = get_settings()
    p = _PREFIX[plane]
    mode = getattr(settings, f"{p}_pool_mode", None) or settings.db_pool_mode
    return PoolConfig(plane=plane, mode=mode, size=getattr(settings, f"{p}_pool_size"),
                      max_overflow=getattr(settings, f"{p}_max_overflow"), timeout=getattr(settings, f"{p}_pool_timeout"),
                      recycle=settings.db_pool_recycle, transaction_pooler=settings.db_transaction_pooler)


def engine_kwargs(plane: Plane, url: str, settings: Any = None) -> dict[str, Any]:
    """Keyword arguments for ``create_engine(url, **kwargs)`` on ``plane``."""
    cfg = pool_config(plane, settings)
    kwargs: dict[str, Any]
    if cfg.mode == "none":
        kwargs = {"poolclass": NullPool}
    else:
        kwargs = {"pool_pre_ping": True, "pool_size": cfg.size, "max_overflow": cfg.max_overflow,
                  "pool_timeout": cfg.timeout, "pool_recycle": cfg.recycle}
    if cfg.transaction_pooler and make_url(url).drivername == "postgresql+psycopg":
        kwargs["connect_args"] = {"prepare_threshold": None}
    return kwargs
