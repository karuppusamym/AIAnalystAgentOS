"""An id column whose values live in several tables' keys (a ticket reference that is an incident, a task or a
change) is reported as a polymorphic reference: measured containment in each table's key, unique disjoint text
keys only, never a single relationship."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.skills.relationships import discover_polymorphic_references, discover_relationships  # noqa: E402
from skills_fixtures import DuckRunSQL  # noqa: E402


def _table(name: str, cols: list[tuple[str, str, bool]]) -> dict:
    return {"asset": name, "columns": [{"name": n, "data_type": t, "is_key": k} for n, t, k in cols]}


@pytest.fixture
def duck():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA sn")
    con.execute("CREATE TABLE sn.incident AS SELECT 'inc' || i AS sys_id FROM range(1, 61) t(i)")
    con.execute("CREATE TABLE sn.sc_task AS SELECT 'tsk' || i AS sys_id FROM range(1, 31) t(i)")
    con.execute("CREATE TABLE sn.change_request AS SELECT 'chg' || i AS sys_id FROM range(1, 11) t(i)")
    con.execute("CREATE TABLE sn.activity AS SELECT 'inc' || (1 + i % 60) AS task_sys_id, 'inc' || (1 + i % 60) AS plain_ref, "
                "'x' || i AS note_id FROM range(0, 100) t(i)")
    con.execute("INSERT INTO sn.activity SELECT 'tsk' || (1 + i % 30), 'zzz', 'y' || i FROM range(0, 60) t(i)")
    con.execute("INSERT INTO sn.activity SELECT 'chg' || (1 + i % 10), 'zzz', 'z' || i FROM range(0, 40) t(i)")
    return DuckRunSQL(con)


ASSETS = [_table("sn.incident", [("sys_id", "VARCHAR", True)]), _table("sn.sc_task", [("sys_id", "VARCHAR", True)]),
          _table("sn.change_request", [("sys_id", "VARCHAR", True)]),
          _table("sn.activity", [("task_sys_id", "VARCHAR", False), ("note_id", "VARCHAR", False)])]


def test_a_column_spread_over_three_ticket_tables_is_one_polymorphic_reference(duck):
    assert not [r for r in discover_relationships(duck, ASSETS) if r.from_column == "task_sys_id"]  # no single table fits
    found = discover_polymorphic_references(duck, ASSETS)
    assert [(f.from_asset, f.from_column) for f in found] == [("sn.activity", "task_sys_id")]
    f = found[0]
    assert f.coverage == 1.0 and f.rows == 200
    assert [(t["asset"], t["rows"]) for t in f.targets] == [("sn.incident", 100), ("sn.sc_task", 60), ("sn.change_request", 40)]
    assert f.targets[0]["share"] == 0.5


def test_a_column_one_table_explains_or_no_table_explains_is_not_polymorphic(duck):
    assert discover_polymorphic_references(duck, ASSETS, exclude={("sn.activity", "task_sys_id")}) == []
    only = [ASSETS[0], ASSETS[3]]
    assert discover_polymorphic_references(duck, only) == []  # one candidate target is a plain reference, not polymorphic


def test_overlapping_keys_are_not_evidence(duck):
    duck.con.execute("CREATE TABLE sn.other AS SELECT 'inc' || i AS sys_id FROM range(1, 61) t(i)")
    assets = [*ASSETS, _table("sn.other", [("sys_id", "VARCHAR", True)])]
    assert discover_polymorphic_references(duck, assets) == []  # the same values sit in two tables: sums exceed the rows


def test_numeric_ids_are_never_polymorphic_evidence(duck):
    duck.con.execute("CREATE TABLE sn.a AS SELECT i AS id FROM range(1, 51) t(i)")
    duck.con.execute("CREATE TABLE sn.b AS SELECT i AS id FROM range(51, 101) t(i)")
    duck.con.execute("CREATE TABLE sn.c AS SELECT i AS ref_id FROM range(1, 101) t(i)")
    assets = [_table("sn.a", [("id", "BIGINT", True)]), _table("sn.b", [("id", "BIGINT", True)]),
              _table("sn.c", [("ref_id", "BIGINT", False)])]
    assert discover_polymorphic_references(duck, assets) == []
