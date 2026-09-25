"""Metadata crawler lifecycle against a real SQLite source and the Postgres control plane.

Proves the properties the crawler exists for: unchanged tables cost nothing on re-crawl, schema
drift is detected precisely, owner curation (tags, descriptions) survives every crawl, and only a
full crawl of what was actually looked at may deprecate a table.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from analystos.core.config import get_settings
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import CrawlRun, Relationship, Source, SourceAsset, SourceColumn, User
from analystos.security.auth import hash_password
from analystos.services.workspaces import create_workspace

pytestmark = pytest.mark.integration


def _make(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE region (id INTEGER PRIMARY KEY, name VARCHAR(40) NOT NULL);
        CREATE TABLE "Sales Orders" (id INTEGER PRIMARY KEY, region_id INTEGER REFERENCES region(id),
                                     "Net Amount" NUMERIC(10,2), sold_on DATE, shipped BOOLEAN);
        CREATE VIEW big_orders AS SELECT * FROM "Sales Orders" WHERE "Net Amount" > 10;
        INSERT INTO region VALUES (1, 'North'), (2, 'South');
        INSERT INTO "Sales Orders" VALUES (1, 1, 12.5, '2025-02-01', 1), (2, 2, 40.0, '2025-02-03', 0);
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def world(control_db):
    upload = Path(get_settings().upload_dir)
    upload.mkdir(parents=True, exist_ok=True)
    fname = f"crawl-{new_id('t')}.db"
    _make(upload / fname)
    with session_scope() as s:
        owner = User(id=new_id("usr"), email=f"owner-{new_id('x')}@t", name="Owner", password_hash=hash_password("x"))
        s.add(owner)
        s.flush()
        ws = create_workspace(s, owner, name="Crawl test", objective="", autonomy_level=3)
        s.flush()
        src = Source(id=new_id("src"), workspace_id=ws.id, kind="sqlite", name="Shop", config={"path": fname},
                     status="registered", execution_mode="staged")
        s.add(src)
        ids = {"ws": ws.id, "owner": owner.id, "src": src.id, "db": upload / fname}
    yield ids
    ids["db"].unlink(missing_ok=True)


def _owner(uid: str) -> User:
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
        return u


def _assets(src: str) -> dict[str, SourceAsset]:
    with session_scope() as s:
        rows = list(s.scalars(select(SourceAsset).where(SourceAsset.source_id == src)))
        s.expunge_all()
        return {a.name: a for a in rows}


def _column(asset_id: str, name: str) -> SourceColumn:
    with session_scope() as s:
        c = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == asset_id, SourceColumn.name == name))
        s.expunge(c)
        return c


def test_crawl_lifecycle_drift_curation_and_deprecation(world):
    from analystos.services.crawler import crawl_source
    from analystos.services.sources import discover_source, tag_column

    owner = _owner(world["owner"])
    # 1. baseline through the discovery entry point (full crawl, no profiling)
    first = discover_source(owner, world["src"])
    assert {a["name"] for a in first["assets"]} == {"region", "sales_orders", "big_orders"}
    assert first["stats"]["new"] == 3 and first["stats"]["model_calls"] == 0
    assets = _assets(world["src"])
    orders = assets["sales_orders"]
    assert orders.fingerprint and orders.lifecycle == "active"
    assert orders.semantics["role"] in ("fact", "event") and orders.semantics["source_key"]
    assert orders.business_name and orders.description and orders.description_origin == "rule"
    assert _column(orders.id, "net_amount").semantics["semantic_role"] in ("amount", "measure")
    with session_scope() as s:
        rels = list(s.scalars(select(Relationship).where(Relationship.workspace_id == world["ws"])))
        assert any(r.from_column == "region_id" and r.origin == "declared" for r in rels)
        assert s.get(Source, world["src"]).status == "discovered"

    # 2. owner curation: a restricted tag and a written description
    region = assets["region"]
    with session_scope() as s:
        tag_column(s, s.get(User, world["owner"]), region.id, "name", ["restricted"])
        a = s.get(SourceAsset, region.id)
        a.description, a.description_origin = "Sales territories used for regional reporting.", "user"

    # 2b. a curated business name survives a full crawl that re-derives semantics
    with session_scope() as s:
        o = s.get(SourceAsset, orders.id)
        o.business_name, o.business_name_origin = "Customer orders", "user"
    crawl_source(owner, world["src"], mode="full")
    assert _assets(world["src"])["sales_orders"].business_name == "Customer orders"

    # 2c. select + stage two tables (the gateway now queries the snapshot)
    from analystos.services.sources import select_assets

    select_assets(owner, world["src"], ["sales_orders", "region"])

    # 3. unchanged re-crawl touches nothing
    again = crawl_source(owner, world["src"], mode="incremental")
    assert again["stats"]["unchanged"] == 3 and again["stats"]["touched"] == 0

    # 4. drift: a PII column is added, the view is dropped
    conn = sqlite3.connect(world["db"])
    conn.executescript('ALTER TABLE "Sales Orders" ADD COLUMN customer_email TEXT; DROP VIEW big_orders;')
    conn.commit()
    conn.close()
    inc = crawl_source(owner, world["src"], mode="incremental", profile=True)
    # the staged snapshot followed the origin, so profiling saw the new column instead of failing
    assert inc["stats"]["restaged"] == 1 and inc["stats"]["profile_errors"] == 0 and inc["stats"]["profiled"] >= 1
    changed = {c["key"].rsplit(".", 1)[-1]: c for c in inc["changes"]["changed"]}
    assert set(changed) == {"sales_orders"} and changed["sales_orders"]["added"] == ["customer_email"]
    assert [k.rsplit(".", 1)[-1] for k in inc["changes"]["missing"]] == ["big_orders"]
    assert inc["changes"]["deprecated"] == []  # an incremental crawl never deprecates
    assert "pii" in _column(orders.id, "customer_email").tags
    assert _column(region.id, "name").tags == ["restricted"] and _column(region.id, "name").tags_origin == "user"
    assert _assets(world["src"])["region"].description == "Sales territories used for regional reporting."

    # 5. a full crawl that excludes a table must not deprecate it; the dropped view is deprecated
    full = crawl_source(owner, world["src"], mode="full", exclude=["region"])
    assert [k.rsplit(".", 1)[-1] for k in full["changes"]["deprecated"]] == ["big_orders"]
    after = _assets(world["src"])
    assert after["big_orders"].lifecycle == "deprecated" and not after["big_orders"].selected
    assert after["region"].lifecycle == "active"
    assert _column(region.id, "name").tags == ["restricted"]

    from analystos.staging.loader import StagingLoader

    StagingLoader(get_settings()).drop_source(world["src"])
    with session_scope() as s:
        runs = list(s.scalars(select(CrawlRun).where(CrawlRun.source_id == world["src"])))
        assert len(runs) == 5 and all(r.status == "succeeded" and r.log for r in runs)
