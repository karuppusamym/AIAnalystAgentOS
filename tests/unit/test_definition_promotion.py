"""P7-03 follow-up (ADR-0021 §5): a published definition is promoted dev -> test -> prod by content hash; the spec is
identical in every environment and only its connection bindings differ."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.contracts.definition import DefinitionDraftIn
from analystos.core.errors import Conflict, InvalidInput, NotFound
from analystos.db.base import session_scope
from analystos.db.models import Definition, Source, User, Workspace, WorkspaceMember
from analystos.services import definitions as defs
from analystos.tools import query_tools as Q

SPEC = {"description": "Open incidents", "sql": "SELECT id FROM sn.incident WHERE priority = :priority",
        "parameters": {"type": "object", "properties": {"priority": {"type": "string"}}, "required": ["priority"]},
        "source_id": "src_dev", "test_arguments": {"priority": "P1"}}


@pytest.fixture
def world(sqlite_db, monkeypatch):
    with session_scope() as s:
        s.add(User(id="usr_o", email="o@pr", name="O", password_hash="x", is_admin=False, active=True, attributes={}))
        for ws, env in (("ws_dev", "dev"), ("ws_test", "test"), ("ws_prod", "prod")):
            s.add(Workspace(id=ws, name=ws, created_by="usr_o", settings={"environment": env}, policy_version=1))
            s.add(WorkspaceMember(workspace_id=ws, user_id="usr_o", role="owner"))
        s.add(Source(id="src_dev", workspace_id="ws_dev", kind="postgres", name="servicenow"))
        s.add(Source(id="src_test", workspace_id="ws_test", kind="postgres", name="servicenow"))
        s.add(Source(id="src_test_csv", workspace_id="ws_test", kind="csv", name="uploads"))
        s.add(Source(id="src_prod_sn", workspace_id="ws_prod", kind="postgres", name="servicenow-prod"))
    monkeypatch.setattr(Q, "run", lambda *a, **k: {"query_id": "q1", "row_count": 1, "result_hash": "rh", "columns": ["id"]})
    with session_scope() as s:
        user = s.get(User, "usr_o")
        s.expunge(user)
        row = defs.create_draft(s, s.merge(user), "ws_dev", DefinitionDraftIn(kind="query_tool", key="open_incidents", spec=SPEC))
        row = defs.test(s, s.merge(user), row, row.revision)
        row = defs.publish(s, s.merge(user), row, row.revision)
        dev_id = row.id
    return SimpleNamespace(user=user, dev_id=dev_id)


def _promote(user, def_id, target, bindings=None):
    with session_scope() as s:
        row = s.get(Definition, def_id)
        new = defs.promote(s, s.merge(user), row, target, bindings=bindings)
        return new.id, new.content_hash, new.spec, new.bindings, new.promoted_from, new.version


def test_promotion_copies_by_content_hash_and_rebinds_connections(world):
    with session_scope() as s:
        dev = s.get(Definition, world.dev_id)
        dev_hash, dev_spec = dev.content_hash, dict(dev.spec)
    test_id, h, spec, bindings, origin, version = _promote(world.user, world.dev_id, "ws_test")
    assert h == dev_hash and spec == dev_spec and spec["source_id"] == "src_dev" and version == 1  # identical, as stored
    assert bindings == {"environment": "test", "sources": {"src_dev": "src_test"}}  # bound by connection name
    assert origin["definition_id"] == world.dev_id and origin["environment"] == "dev"
    with session_scope() as s:
        ref, runnable = defs.resolve_runnable(s, "ws_test", {"kind": "query_tool", "key": "open_incidents"}, trigger="mcp")
        assert ref.content_hash == dev_hash and runnable["source_id"] == "src_test"  # runs on the test connection
        assert s.get(Definition, test_id).test_evidence["query_id"] == "q1"
    # promoting the same content again returns the version already there
    assert _promote(world.user, world.dev_id, "ws_test")[0] == test_id
    # test -> prod needs an explicit binding (no connection of the same name there)
    with pytest.raises(InvalidInput, match="bind it explicitly"):
        _promote(world.user, test_id, "ws_prod")
    prod_id, h2, _, b2, o2, _ = _promote(world.user, test_id, "ws_prod", {"src_dev": "src_prod_sn"})
    assert h2 == dev_hash and b2["sources"] == {"src_dev": "src_prod_sn"} and o2["environment"] == "test"
    # a binding may also name the connection the source environment runs on
    assert _promote(world.user, test_id, "ws_prod", {"src_test": "src_prod_sn"})[0] == prod_id
    with session_scope() as s:
        assert defs.resolve(s, "ws_prod", {"kind": "query_tool", "id": prod_id})[1]["source_id"] == "src_prod_sn"


def test_promotion_is_one_step_up_from_a_published_version(world):
    with pytest.raises(Conflict, match="dev -> test -> prod"):
        _promote(world.user, world.dev_id, "ws_prod")
    with session_scope() as s:
        row = s.get(Definition, world.dev_id)
        defs.deprecate(s, s.merge(world.user), row, reason="old")
    with pytest.raises(Conflict, match="only a published version"):
        _promote(world.user, world.dev_id, "ws_test")


def test_bindings_must_exist_there_and_keep_the_connection_kind(world):
    with pytest.raises(InvalidInput, match="published against postgres"):
        _promote(world.user, world.dev_id, "ws_test", {"src_dev": "src_test_csv"})
    with pytest.raises(InvalidInput, match="not a connection of the target"):
        _promote(world.user, world.dev_id, "ws_test", {"src_dev": "src_prod_sn"})
    with pytest.raises(InvalidInput, match="does not use"):
        _promote(world.user, world.dev_id, "ws_test", {"src_dev": "src_test", "src_other": "src_test"})


def test_a_caller_without_a_role_in_the_target_cannot_promote(world):
    with session_scope() as s:
        s.add(Workspace(id="ws_other", name="o", created_by="x", settings={"environment": "test"}, policy_version=1))
    with pytest.raises(NotFound):
        _promote(world.user, world.dev_id, "ws_other")


def test_referenced_sources_and_bindings_walk_nested_specs():
    spec = {"dataset": {"source_id": "a"}, "inputs": [{"source_ids": ["a", "b"]}], "x": 1}
    assert defs.referenced_sources(spec) == ["a", "b"]
    assert defs.apply_bindings(spec, {"a": "A"}) == {"dataset": {"source_id": "A"}, "inputs": [{"source_ids": ["A", "b"]}],
                                                     "x": 1}
