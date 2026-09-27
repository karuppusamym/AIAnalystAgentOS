"""The brief reads the profile key contract and the catalog's time roles (Stream A audit items 1-2): only a key
that is unique on the data is suggested, a measured key check decides uniqueness (composite keys too), and
date/timestamp roles make an event time. The review queue does not ask again about a decided measurement."""
from __future__ import annotations

from sqlalchemy import select
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.db.base import session_scope
from analystos.db.models import SourceAsset, SourceColumn
from analystos.services import brief as brief_svc


def _derive():
    with session_scope() as s:
        return {a.key: a for a in brief_svc.derive(s, WS)}


def _undeclare(asset_id: str) -> None:
    with session_scope() as s:
        for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset_id)):
            c.is_key = False


def test_only_unique_profile_keys_are_suggested(world):  # noqa: F811
    _undeclare("ast_lines")
    with session_scope() as s:
        s.get(SourceAsset, "ast_lines").stats = {"candidate_keys": [
            {"columns": ["order_id"], "column": "order_id", "unique": False, "evidence": "name_hint", "distinct": 100},
            {"columns": ["id"], "column": "id", "unique": True, "evidence": "profile_unique"}]}
    key = _derive()["data_semantics.entity_key:sales.order_line"]
    assert key.value == ["id"] and key.origin == "rule"
    with session_scope() as s:
        s.get(SourceAsset, "ast_lines").stats = {"candidate_keys": [
            {"columns": ["order_id"], "column": "order_id", "unique": False, "evidence": "name_hint"}]}
    assert "data_semantics.entity_key:sales.order_line" not in _derive()


def test_a_measured_key_check_decides_composite_uniqueness(world):  # noqa: F811
    with session_scope() as s:
        s.get(SourceAsset, "ast_lines").stats = {"key_check": {"columns": ["order_id", "id"], "rows": 400,
                                                               "distinct_keys": 400, "unique": True}}
        assert brief_svc.key_uniqueness(s, WS, "sales.order_line", ["id", "order_id"])["state"] == "unique"
        s.get(SourceAsset, "ast_lines").stats = {"key_check": {"columns": ["order_id", "id"], "rows": 400,
                                                               "distinct_keys": 390, "unique": False},
                                                 "candidate_keys": [{"columns": ["order_id"], "unique": False,
                                                                     "distinct": 100, "null_count": 0}]}
        assert brief_svc.key_uniqueness(s, WS, "sales.order_line", ["order_id", "id"])["state"] == "duplicates"
        # a profile that saw duplicates answers without needing the column's own profile
        assert brief_svc.key_uniqueness(s, WS, "sales.order_line", ["order_id"])["state"] == "duplicates"


def test_event_time_comes_from_date_and_timestamp_roles(world):  # noqa: F811
    with session_scope() as s:
        for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == "ast_orders")):
            c.semantic_type = None  # not profiled yet: only the catalog role is known
            c.semantics = {"semantic_role": "timestamp"} if c.name == "ordered_at" else {}
        s.add(SourceColumn(asset_id="ast_lines", name="ship_date", ordinal=9, data_type="date", tags=[], profile={},
                           semantics={"semantic_role": "date"}))
    got = _derive()
    assert got["time_measures.event_time:sales.orders"].value == "ordered_at"
    assert got["time_measures.event_time:sales.order_line"].value == "ship_date"
