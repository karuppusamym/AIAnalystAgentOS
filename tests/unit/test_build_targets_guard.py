"""M4: the build login never runs on the well-known development password outside development."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.build import targets
from analystos.core.config import Settings
from analystos.core.errors import InvalidInput

DEFAULT = Settings.model_fields["analytics_builder_url"].default


def _settings(env: str, url: str = DEFAULT) -> SimpleNamespace:
    return SimpleNamespace(env=env, analytics_builder_url=url,
                           analytics_loader_url="postgresql+psycopg://analystos_loader:x@db:5432/analytics")


@pytest.mark.parametrize("url", [DEFAULT, "postgresql+psycopg://analystos_builder@db:5432/analytics",
                                 "postgresql+psycopg://analystos_builder:builder@db.prod:5432/analytics"])
def test_default_or_missing_builder_password_is_refused_outside_dev(url):
    with pytest.raises(InvalidInput, match="development password"):
        targets.check_builder_credentials(_settings("production", url))


def test_dev_and_a_real_secret_pass():
    targets.check_builder_credentials(_settings("dev"))
    targets.check_builder_credentials(_settings("production", "postgresql+psycopg://analystos_builder:s3cr3t-Xy@db:5432/analytics"))


def test_provisioning_and_the_build_gateway_refuse_before_connecting(monkeypatch):
    monkeypatch.setattr(targets.psycopg, "connect", lambda *a, **k: pytest.fail("must refuse before connecting"))
    with pytest.raises(InvalidInput, match="development password"):
        targets.ensure_builder_login(_settings("production"))
    from analystos.build.gateway import BuildGateway

    settings = SimpleNamespace(**vars(_settings("production")), analytics_reader_url="postgresql+psycopg://r:x@db/analytics",
                               database_url="postgresql+psycopg://c:x@ctl/analystos", dbt_executable="dbt",
                               build_timeout_seconds=60)
    with pytest.raises(InvalidInput, match="development password"):
        BuildGateway(settings)
