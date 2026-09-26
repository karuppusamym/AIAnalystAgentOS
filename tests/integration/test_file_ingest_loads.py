"""P6-06 on the compose Postgres: files (CSV, JSON array, NDJSON, Excel, Parquet) ingested into a staged
file source with replace, append and merge on the Arrow + COPY loader. Merge is idempotent on retry;
null keys, repeated keys and column mismatches are refused with the column named and nothing written;
the content fingerprint changes only when the content does, whatever mode wrote it; the table is read
back through the query gateway under the workspace reader role, and the API binds the source to its
workspace. Skips cleanly without the stack."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.sources import register_source
    from analystos.services.workspaces import create_workspace

    get_settings.cache_clear()
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        ws = create_workspace(s, owner, name=f"ingest {new_id('t')}", objective="", autonomy_level=3)
        s.flush()
        folder = Path(get_settings().upload_dir) / ws.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "placeholder.csv").write_text("a\n1\n")
        src = register_source(s, owner, ws.id, kind="csv", name="uploads", config={"path": ws.id}, secret_ref=None)
        s.flush()
        ids = {"ws": ws.id, "src": src.id, "folder": folder}
        s.expunge(owner)
    ids["owner"] = owner
    yield ids
    shutil.rmtree(ids["folder"], ignore_errors=True)


def _csv(world, name: str, rows: list[dict]) -> str:
    pl.DataFrame(rows).write_csv(world["folder"] / name)
    return name


def _ingest(world, **spec):
    from analystos.services.file_ingest import IngestSpec, ingest_file

    return ingest_file(world["owner"], world["src"], IngestSpec(**spec), world["ws"])


def _read(world, asset: str) -> list[list]:
    from analystos.db.base import session_scope
    from analystos.governance.policy import resolve_scope
    from analystos.runtime.context import default_gateway

    with session_scope() as s:
        scope = resolve_scope(s, world["owner"], world["ws"])
    runner = default_gateway().run_sql_for(scope, actor="test", source_id=world["src"])
    return runner(f"SELECT * FROM {asset} ORDER BY 1", purpose="test", use_cache=False).rows


ORDERS = [{"Order ID": 1, "Customer": "Ada", "Amount": 12.5}, {"Order ID": 2, "Customer": "Bo", "Amount": 7.25}]
MAPPING = [{"source": "Order ID", "target": "order_id", "type": "bigint"}, {"source": "Customer"},
           {"source": "Amount", "type": "numeric(10,2)"}]


def test_merge_is_idempotent_and_the_fingerprint_follows_content(world):
    first = _ingest(world, path=_csv(world, "orders_v1.csv", ORDERS), table="orders", mapping=MAPPING)
    assert first["row_count"] == 2 and first["changed"] is True
    asset = first["asset"]
    # the same content merged again: same table, same fingerprint, no new data version
    again = _ingest(world, path="orders_v1.csv", table="orders", mode="merge", keys=["order_id"], mapping=MAPPING)
    assert (again["row_count"], again["replaced_rows"], again["changed"]) == (2, 2, False)
    assert again["content_fingerprint"] == first["content_fingerprint"]
    # changed content: one row updated, one added
    v2 = [{"Order ID": 2, "Customer": "Bo", "Amount": 9.0}, {"Order ID": 3, "Customer": "Cy", "Amount": 1.0}]
    changed = _ingest(world, path=_csv(world, "orders_v2.csv", v2), table="orders", mode="merge", keys=["order_id"],
                      mapping=MAPPING)
    assert changed["row_count"] == 3 and changed["changed"] is True
    assert changed["content_fingerprint"] != first["content_fingerprint"]
    retry = _ingest(world, path="orders_v2.csv", table="orders", mode="merge", keys=["order_id"], mapping=MAPPING)
    assert retry["row_count"] == 3 and retry["content_fingerprint"] == changed["content_fingerprint"]
    rows = _read(world, asset)
    assert [(r[0], r[1], float(r[2])) for r in rows] == [(1, "Ada", 12.5), (2, "Bo", 9.0), (3, "Cy", 1.0)]
    # replace with the same final content reaches the same fingerprint as the merges did
    final = [{"Order ID": 1, "Customer": "Ada", "Amount": 12.5}, {"Order ID": 3, "Customer": "Cy", "Amount": 1.0},
             {"Order ID": 2, "Customer": "Bo", "Amount": 9.0}]
    replaced = _ingest(world, path=_csv(world, "orders_final.csv", final), table="orders", mapping=MAPPING)
    assert replaced["content_fingerprint"] == retry["content_fingerprint"] and replaced["changed"] is False
    # append adds rows (content changes)
    appended = _ingest(world, path=_csv(world, "orders_more.csv", [{"Order ID": 4, "Customer": "Di", "Amount": 2.0}]),
                       table="orders", mode="append", mapping=MAPPING)
    assert appended["row_count"] == 4 and appended["changed"] is True


def test_null_keys_repeated_keys_and_column_mismatches_are_refused_with_the_column(world):
    from analystos.core.errors import InvalidInput

    base = _ingest(world, path=_csv(world, "base.csv", ORDERS), table="guarded", mapping=MAPPING)
    null_key = [{"Order ID": None, "Customer": "Zed", "Amount": 1.0}, {"Order ID": 5, "Customer": "Y", "Amount": 1.0}]
    with pytest.raises(InvalidInput) as err:
        _ingest(world, path=_csv(world, "nullkey.csv", null_key), table="guarded", mode="merge", keys=["order_id"],
                mapping=MAPPING)
    assert "merge key column order_id is null in row 1" in err.value.message
    dup = [{"Order ID": 7, "Customer": "A", "Amount": 1.0}, {"Order ID": 7, "Customer": "B", "Amount": 2.0}]
    with pytest.raises(InvalidInput, match=r"merge key \(order_id\) repeats in 1 key value"):
        _ingest(world, path=_csv(world, "dup.csv", dup), table="guarded", mode="merge", keys=["order_id"], mapping=MAPPING)
    extra = [{"Order ID": 8, "Customer": "C", "Amount": 1.0, "Region": "north"}]
    with pytest.raises(InvalidInput) as err:
        _ingest(world, path=_csv(world, "extra.csv", extra), table="guarded", mode="append")
    assert "column region" in err.value.message and "is not in the staged table" in err.value.message
    retyped = [{"Order ID": 9, "Customer": "C", "Amount": "n/a"}]
    with pytest.raises(InvalidInput) as err:
        _ingest(world, path=_csv(world, "retyped.csv", retyped), table="guarded", mode="append",
                mapping=[{"source": "Order ID", "target": "order_id", "type": "bigint"}, {"source": "Customer"},
                         {"source": "Amount"}])
    assert "column amount is numeric(10,2) in the staged table but text" in err.value.message
    with pytest.raises(InvalidInput, match="'Price' is not in the file"):
        _ingest(world, path="base.csv", table="guarded", mapping=[{"source": "Price"}])
    # nothing was written by any refused load
    assert [(r[0], r[1]) for r in _read(world, base["asset"])] == [(1, "Ada"), (2, "Bo")]


def test_json_excel_and_parquet(world):
    rows = [{"id": 1, "tags": "a", "when": "2024-04-01"}, {"id": 2, "tags": "b", "when": "2024-04-02"}]
    (world["folder"] / "events.json").write_text(json.dumps(rows))
    (world["folder"] / "events.ndjson").write_text("\n".join(json.dumps(r) for r in rows[:1]))
    pl.DataFrame(rows).write_parquet(world["folder"] / "events.parquet")
    mapping = [{"source": "id", "type": "integer"}, {"source": "tags"}, {"source": "when", "target": "event_date",
                                                                          "type": "date"}]
    a = _ingest(world, path="events.json", table="events", mapping=mapping)
    b = _ingest(world, path="events.parquet", table="events", mode="merge", keys=["id"], mapping=mapping)
    assert a["format"] == "json" and b["format"] == "parquet" and b["changed"] is False
    c = _ingest(world, path="events.ndjson", table="events_nd", mapping=mapping)
    assert c["row_count"] == 1
    pytest.importorskip("openpyxl")
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "data"
    wb.active.append(["id", "tags", "when"])
    for r in rows:
        wb.active.append(list(r.values()))
    wb.save(world["folder"] / "events.xlsx")
    d = _ingest(world, path="events.xlsx", sheet="data", table="events", mode="merge", keys=["id"], mapping=mapping)
    assert d["format"] == "excel" and d["content_fingerprint"] == a["content_fingerprint"]
    assert [str(r[2]) for r in _read(world, a["asset"])] == ["2024-04-01", "2024-04-02"]


def test_ingest_api_binds_the_source_to_its_workspace(world, control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as api:
        r = api.post("/api/auth/login", json={"email": "analyst@analystos.local", "password": PASSWORD})
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        name = _csv(world, "api.csv", ORDERS)
        ok = api.post(f"/api/workspaces/{world['ws']}/sources/{world['src']}/ingest", headers=h,
                      json={"path": name, "table": "api_orders", "mapping": MAPPING})
        assert ok.status_code == 200, ok.text
        assert ok.json()["row_count"] == 2
        other = api.post("/api/workspaces", headers=h, json={"name": "other ingest"}).json()["id"]
        miss = api.post(f"/api/workspaces/{other}/sources/{world['src']}/ingest", headers=h,
                        json={"path": name, "table": "api_orders"})
        assert miss.status_code == 404
        outside = api.post(f"/api/workspaces/{world['ws']}/sources/{world['src']}/ingest", headers=h,
                           json={"path": "../../etc/passwd", "table": "x"})
        assert outside.status_code == 422 and "upload directory" in outside.text
