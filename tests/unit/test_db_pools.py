"""Pool settings per database plane (P4-S05): env -> Settings -> engine kwargs -> the engines."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.pool import NullPool, QueuePool

from analystos.core.config import Settings
from analystos.db.pools import engine_kwargs, pool_config

PG = "postgresql+psycopg://u:p@db.invalid:5432/x"


def test_defaults_keep_the_previous_control_and_analytics_pools():
    s = Settings(_env_file=None)
    control, analytics, loader = (pool_config(p, s) for p in ("control", "analytics", "loader"))
    assert (control.mode, control.size, control.max_overflow) == ("queue", 10, 20)
    assert (analytics.mode, analytics.size, analytics.max_overflow) == ("queue", 5, 5)
    assert loader.max_connections == 5  # lowered from 10: one COPY per staging step
    assert engine_kwargs("control", PG, s) == {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20,
                                               "pool_timeout": 30.0, "pool_recycle": -1}


def test_environment_sets_each_plane(monkeypatch):
    for k, v in {"ANALYSTOS_DB_POOL_SIZE": "3", "ANALYSTOS_DB_MAX_OVERFLOW": "1", "ANALYSTOS_DB_POOL_TIMEOUT": "7.5",
                 "ANALYSTOS_DB_POOL_RECYCLE": "280", "ANALYSTOS_ANALYTICS_POOL_SIZE": "2",
                 "ANALYSTOS_ANALYTICS_MAX_OVERFLOW": "0", "ANALYSTOS_LOADER_POOL_MODE": "none"}.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert engine_kwargs("control", PG, s) == {"pool_pre_ping": True, "pool_size": 3, "max_overflow": 1,
                                               "pool_timeout": 7.5, "pool_recycle": 280}
    a = pool_config("analytics", s)
    assert (a.mode, a.max_connections, a.recycle) == ("queue", 2, 280)
    assert engine_kwargs("loader", PG, s) == {"poolclass": NullPool}


def test_none_mode_is_inherited_unless_a_plane_overrides_it(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_DB_POOL_MODE", "none")
    monkeypatch.setenv("ANALYSTOS_ANALYTICS_POOL_MODE", "queue")
    s = Settings(_env_file=None)
    assert pool_config("control", s).mode == "none"
    assert pool_config("loader", s).mode == "none"
    assert pool_config("analytics", s).mode == "queue"
    assert pool_config("control", s).max_connections is None


def test_transaction_pooler_turns_off_server_side_prepares_for_psycopg_only(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_DB_TRANSACTION_POOLER", "true")
    s = Settings(_env_file=None)
    assert engine_kwargs("control", PG, s)["connect_args"] == {"prepare_threshold": None}
    assert "connect_args" not in engine_kwargs("analytics", "mysql+pymysql://u:p@h/db", s)
    assert "connect_args" not in engine_kwargs("control", PG, Settings(_env_file=None, db_transaction_pooler=False))


@pytest.mark.parametrize("env", [{"ANALYSTOS_DB_POOL_MODE": "session"}, {"ANALYSTOS_DB_POOL_SIZE": "0"},
                                 {"ANALYSTOS_LOADER_MAX_OVERFLOW": "-1"}])
def test_invalid_pool_settings_are_refused(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_unknown_plane_is_refused():
    with pytest.raises(ValueError, match="plane"):
        pool_config("builder", Settings(_env_file=None))  # type: ignore[arg-type]


def test_engines_are_built_with_their_plane(monkeypatch):
    """No connection is opened: create_engine is lazy."""
    from analystos.core import config
    from analystos.db import base
    from analystos.gateway import engines

    s = Settings(_env_file=None, db_pool_size=4, db_max_overflow=2, analytics_pool_size=3, loader_pool_mode="none")
    monkeypatch.setattr(config, "get_settings", lambda: s)
    control = base.get_engine("postgresql+psycopg://u:p@db.invalid:5432/control_s05_unit")
    try:
        assert isinstance(control.pool, QueuePool)
        assert (control.pool.size(), control.pool._max_overflow) == (4, 2)
        reader = engines.get_engine("postgresql+psycopg://r:p@db.invalid:5432/analytics_s05_unit")
        loader = engines.get_engine("postgresql+psycopg://l:p@db.invalid:5432/analytics_s05_unit", plane="loader")
        assert isinstance(reader.pool, QueuePool) and reader.pool.size() == 3
        assert isinstance(loader.pool, NullPool)
        assert engines.get_engine("postgresql+psycopg://r:p@db.invalid:5432/analytics_s05_unit") is reader
    finally:
        control.dispose()
        base.get_engine.cache_clear()
        base._factory.cache_clear()
        engines.dispose_all()


def test_no_session_level_role_switch_on_pooled_planes():
    """Role switches must be SET LOCAL (transaction-scoped): a session-level SET ROLE would survive
    on a server connection a transaction-mode pooler hands to the next client."""
    src = Path(__file__).resolve().parents[2] / "src" / "analystos"
    offenders = [f"{p.relative_to(src)}:{i}" for p in src.rglob("*.py")
                 for i, line in enumerate(p.read_text().splitlines(), 1)
                 if re.search(r"execute\(.*[\"']SET (SESSION )?ROLE\b", line)]
    assert offenders == []
