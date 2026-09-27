"""Stream A crawler behaviour on a real SQLite source and the Postgres control plane: a governed crawl measures the
declared reference (validated only because the measurement corroborates it), stores format masks of non-sensitive
columns only, sanitizes a PII column's profile, records profile_meta; a renamed table leaves scope and a person's
curation moves to the new name."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from analystos.core.config import get_settings
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Relationship, SourceAsset, SourceColumn, User
from analystos.security.auth import hash_password
from analystos.services.workspaces import create_workspace

pytestmark = pytest.mark.integration


def _make(path: Path) -> None:
    conn = sqlite3.connect(path)
    rows = ",".join(f"({i}, {1 + i % 5}, 'AB-{i:03d}', {i * 2.5})" for i in range(1, 41))
    conn.executescript(
        f"""
        CREATE TABLE customer (id INTEGER PRIMARY KEY, name VARCHAR(40) NOT NULL, email VARCHAR(80));
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customer(id), code VARCHAR(10),
                             amount NUMERIC(10,2));
        INSERT INTO customer VALUES (1,'Ann','ann@example.com'),(2,'Bo','bo@example.com'),(3,'Cy','cy@example.com'),
                                    (4,'Di','di@example.com'),(5,'Ed','ed@example.com');
        INSERT INTO orders VALUES {rows};
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def world(control_db):
    upload = Path(get_settings().upload_dir)
    upload.mkdir(parents=True, exist_ok=True)
    fname = f"ctx-{new_id('t')}.db"
    _make(upload / fname)
    with session_scope() as s:
        owner = User(id=new_id("usr"), email=f"owner-{new_id('x')}@t", name="Owner", password_hash=hash_password("x"))
        s.add(owner)
        s.flush()
        ws = create_workspace(s, owner, name="Crawl context", objective="", autonomy_level=3)
        s.flush()
        from analystos.db.models import Source

        src = Source(id=new_id("src"), workspace_id=ws.id, kind="sqlite", name="Shop", config={"path": fname},
                     status="registered", execution_mode="staged")
        s.add(src)
        ids = {"ws": ws.id, "owner": owner.id, "src": src.id, "db": upload / fname}
    yield ids
    from analystos.staging.loader import StagingLoader

    StagingLoader(get_settings()).drop_source(ids["src"])
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


def _cols(asset_id: str) -> dict[str, SourceColumn]:
    with session_scope() as s:
        rows = list(s.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset_id)))
        s.expunge_all()
        return {c.name: c for c in rows}


def test_governed_crawl_measures_references_masks_and_carries_curation_across_a_rename(world):
    from analystos.services.crawler import crawl_source
    from analystos.services.sources import curate_column, discover_source, select_assets

    owner = _owner(world["owner"])
    discover_source(owner, world["src"])
    with session_scope() as s:
        rel = s.scalar(select(Relationship).where(Relationship.workspace_id == world["ws"]))
        assert rel.origin == "declared" and not rel.validated  # declared, not yet measured
    out = select_assets(owner, world["src"], ["customer", "orders"])
    assert out["profile_crawl"]["status"] == "not_requested"  # no scheduler given: the API passes BackgroundTasks
    crawl = crawl_source(owner, world["src"], mode="incremental", profile=True)
    assert crawl["stats"]["profiled"] == 2 and crawl["stats"]["relationships_measured"] == 1, _log(crawl["crawl_id"])
    with session_scope() as s:
        rel = s.scalar(select(Relationship).where(Relationship.workspace_id == world["ws"]))
        assert rel.validated and rel.cardinality == "many_to_one" and rel.evidence["containment"] == 1.0
        assert rel.evidence["assessment"] == "corroborated" and "sql" not in rel.evidence
    assets = _assets(world["src"])
    assert assets["orders"].stats["profile_meta"]["fingerprint"] == assets["orders"].fingerprint
    orders, customer = _cols(assets["orders"].id), _cols(assets["customer"].id)
    assert orders["code"].profile["patterns"] == [{"mask": "AA-999", "share": 1.0}]
    email = customer["email"]
    assert "pii" in email.tags and "patterns" not in email.profile and "top_values" not in email.profile
    assert set(email.profile) <= {"name", "data_type", "type_family", "semantic_type", "is_key", "non_null", "null_count",
                                  "null_rate", "distinct", "distinct_ratio"}
    assert orders["customer_id"].description_origin == "rule" and "Reference to customer" in orders["customer_id"].description

    # a person's curation, then the table is renamed at the source
    with session_scope() as s:
        curate_column(s, s.get(User, world["owner"]), assets["orders"].id, "code",
                      {"business_name": "Order code", "description": "Code printed on the invoice."})
    conn = sqlite3.connect(world["db"])
    conn.executescript("ALTER TABLE orders RENAME TO sales_orders;")
    conn.commit()
    conn.close()
    full = crawl_source(owner, world["src"], mode="full")
    assert full["stats"]["renamed"] == 1 and full["stats"]["renamed_curation_carried"] == 1
    after = _assets(world["src"])
    old, new = after["orders"], after["sales_orders"]
    assert not old.selected and old.lifecycle == "deprecated" and old.semantics["renamed_to"].endswith("sales_orders")
    assert new.semantics["renamed_from"].endswith("orders") and not new.selected
    code = _cols(new.id)["code"]
    assert (code.business_name, code.description, code.description_origin) == (
        "Order code", "Code printed on the invoice.", "user")


def _log(crawl_id: str) -> list:
    from analystos.db.models import CrawlRun

    with session_scope() as s:
        return [e["message"] for e in s.get(CrawlRun, crawl_id).log if e["stage"] in ("relationships", "profile")]
