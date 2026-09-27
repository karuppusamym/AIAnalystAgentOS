"""A person's column business name and description (origin `user`) survive every crawl, even when the
source itself documents the column; the curation needs the same role as column tagging."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.unit.step_fixtures import world  # noqa: F401

from analystos.connectors.base import ConnectionTest, DiscoveredAsset, DiscoveredColumn
from analystos.core.errors import Forbidden, NotFound
from analystos.db.base import session_scope
from analystos.db.models import SourceColumn, User
from analystos.services.crawler import crawl_source
from analystos.services.sources import curate_column


class _Documented:
    """A source whose catalog documents every column (business name and description)."""

    def test(self) -> ConnectionTest:
        return ConnectionTest(ok=True, message="ok")

    def discover(self) -> list[DiscoveredAsset]:
        cols = [DiscoveredColumn(name=n, data_type=t, description=f"Source text for {n}.", business_name=f"Source {n}")
                for n, t in (("id", "integer"), ("state", "text"), ("amount", "numeric"))]
        return [DiscoveredAsset(source_name="orders", name="orders", schema_name="sales", columns=cols)]


@pytest.fixture
def crawlable(world, sqlite_db, monkeypatch):  # noqa: F811
    from analystos.db import models

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in (
        "crawl_run", "knowledge_pack", "knowledge_document")])  # reviewed domain rules are read from the pack
    monkeypatch.setattr("analystos.connectors.registry.build_connector", lambda *a, **k: _Documented())
    return world


def _col(name: str) -> SourceColumn:
    with session_scope() as s:
        c = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == "ast_orders", SourceColumn.name == name))
        s.expunge(c)
        return c


def _crawl(owner: User) -> None:
    out = crawl_source(owner, "src_sales", mode="full", profile=False, enrich=False)
    assert out["stats"]["touched"] == 1


def test_a_user_description_and_business_name_survive_a_crawl_of_a_documented_source(crawlable):
    owner = crawlable["owner"]
    _crawl(owner)
    assert _col("state").description == "Source text for state." and _col("state").description_origin == "source"
    with session_scope() as s:
        me = s.get(User, owner.id)
        curate_column(s, me, "ast_orders", "state", {"business_name": "Order status",
                                                     "description": "Where the order is in fulfilment."})
        curate_column(s, me, "ast_orders", "amount", {"description": ""})  # cleared on purpose
    _crawl(owner)
    state, amount = _col("state"), _col("amount")
    assert (state.business_name, state.business_name_origin) == ("Order status", "user")
    assert (state.description, state.description_origin) == ("Where the order is in fulfilment.", "user")
    assert (amount.description, amount.description_origin) == (None, "user")
    assert _col("id").description == "Source text for id."  # uncurated columns still follow the source


def test_column_curation_needs_the_column_tagging_role(crawlable):
    with session_scope() as s:
        with pytest.raises(Forbidden):
            curate_column(s, s.get(User, crawlable["analyst"].id), "ast_orders", "state", {"description": "x"})
        with pytest.raises(NotFound):
            curate_column(s, s.get(User, crawlable["owner"].id), "ast_orders", "nope", {"description": "x"})
