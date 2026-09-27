"""P7-11 remaining: query tools as definitions (draft -> tested -> published -> retired) with parameters bound as
typed literals and validated by JSON Schema; neither a draft query tool nor a draft HTTP tool is callable over
MCP (absent from the tool list, refused by name)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.definition import DefinitionDraftIn, DefinitionPatch
from analystos.core.errors import Conflict, Forbidden, InvalidInput, PolicyDenied
from analystos.db.base import session_scope
from analystos.db.models import Definition, User, Workspace, WorkspaceMember
from analystos.mcp import grants as G
from analystos.services import definitions as defs
from analystos.tools import query_tools as Q

WS = "ws_qt"
SPEC = {"description": "Open incidents of a priority", "sql": "SELECT id, opened_at FROM sn.incident "
        "WHERE priority = :priority AND reopen_count >= :min_reopens",
        "parameters": {"type": "object", "properties": {"priority": {"type": "string", "enum": ["P1", "P2"]},
                                                         "min_reopens": {"type": "integer", "minimum": 0}},
                       "required": ["priority", "min_reopens"], "additionalProperties": False},
        "max_rows": 50, "test_arguments": {"priority": "P1", "min_reopens": 0}}


@pytest.fixture
def world(sqlite_db, monkeypatch):
    with session_scope() as s:
        s.add(User(id="usr_o", email="o@qt", name="O", password_hash="x", is_admin=False, active=True, attributes={}))
        s.add(Workspace(id=WS, name="qt", created_by="usr_o", settings={}, policy_version=1))
        s.add(WorkspaceMember(workspace_id=WS, user_id="usr_o", role="owner"))
    runs: list[dict] = []

    def fake_run(user, workspace_id, key, spec, arguments, *, actor, purpose):
        sql = Q.bind(spec, arguments, "postgres")
        runs.append({"sql": sql, "actor": actor, "purpose": purpose, "key": key})
        return {"tool": key, "sql": sql, "query_id": f"q{len(runs)}", "columns": ["id"], "rows": [[1]], "row_count": 1,
                "truncated": False, "result_hash": "rh"}

    monkeypatch.setattr(Q, "run", fake_run)
    with session_scope() as s:
        user = s.get(User, "usr_o")
        s.expunge(user)
    return SimpleNamespace(user=user, runs=runs)


# ------------------------------------------------------------------------------------ spec and binding
@pytest.mark.parametrize("change,match", [
    ({"sql": "SELECT 1 FROM t WHERE a = :a"}, "same set"),
    ({"sql": "SELECT 1 FROM t WHERE a = ? AND b = :priority AND c = :min_reopens"}, "named placeholders"),
    ({"sql": "SELECT 1 FROM t; SELECT 2 FROM u"}, "exactly one SELECT"),
    ({"sql": "DELETE FROM t WHERE a = :priority AND b = :min_reopens"}, "exactly one SELECT"),
    ({"parameters": {"type": "object", "properties": {"priority": {"type": "array"}, "min_reopens": {"type": "integer"}}}},
     "scalar type"),
    ({"max_rows": 5000}, "max_rows"),
])
def test_a_query_tool_spec_is_checked(change, match):
    with pytest.raises(InvalidInput, match=match):
        Q.validate_spec(None, WS, "open_incidents", {**SPEC, **change})


def test_arguments_are_schema_checked_and_bound_as_literals():
    sql = Q.bind(SPEC, {"priority": "P1", "min_reopens": 2}, "postgres")
    assert "priority = 'P1'" in sql and "reopen_count >= 2" in sql and ":" not in sql
    injected = {**SPEC, "parameters": {**SPEC["parameters"], "properties": {
        "priority": {"type": "string"}, "min_reopens": {"type": "integer"}}}}
    sql = Q.bind(injected, {"priority": "x' OR '1'='1", "min_reopens": 0}, "postgres")
    assert "priority = 'x'' OR ''1''=''1'" in sql  # a string argument stays one literal
    for bad in ({"priority": "P9", "min_reopens": 0}, {"priority": "P1"}, {"priority": "P1", "min_reopens": "2"},
                {"priority": "P1", "min_reopens": 0, "extra": 1}):
        with pytest.raises(InvalidInput):
            Q.bind(SPEC, bad, "postgres")


# ------------------------------------------------------------------------------------ lifecycle
def _draft(s, user, spec=SPEC) -> Definition:
    return defs.create_draft(s, s.merge(user), WS, DefinitionDraftIn(kind="query_tool", key="open_incidents", spec=spec))


def test_draft_tested_published_retired(world):
    with session_scope() as s:
        row = _draft(s, world.user)
        with pytest.raises(Conflict, match="must pass its test"):
            defs.publish(s, s.merge(world.user), row, row.revision)
        row = defs.test(s, s.merge(world.user), row, row.revision)
        assert row.status == "tested" and row.test_evidence["content_hash"] == row.content_hash
        assert row.test_evidence["query_id"] == "q1" and world.runs[0]["purpose"] == "query_tool.test:open_incidents"
        with pytest.raises(Conflict, match="already has a draft"):  # a tested draft is still the one draft
            _draft(s, world.user)
        with pytest.raises(PolicyDenied, match="draft"):  # outside dev a tested draft does not run
            defs.resolve_runnable(s, WS, {"kind": "query_tool", "id": row.id}, trigger="mcp")
        # editing the tested draft makes it a draft again; publishing then needs a new test
        row = defs.update_draft(s, s.merge(world.user), row, DefinitionPatch(spec={**SPEC, "max_rows": 20}), row.revision)
        assert row.status == "draft"
        with pytest.raises(Conflict, match="must pass its test"):
            defs.publish(s, s.merge(world.user), row, row.revision)
        row = defs.test(s, s.merge(world.user), row, row.revision, arguments={"priority": "P2", "min_reopens": 1})
        row = defs.publish(s, s.merge(world.user), row, row.revision)
        assert row.status == "published" and defs.out(row)["test_evidence"]["arguments"] == {"priority": "P2", "min_reopens": 1}
        assert defs.resolve_runnable(s, WS, {"kind": "query_tool", "key": "open_incidents"}, trigger="mcp")[0].version == 1
        row = defs.retire(s, s.merge(world.user), row, reason="replaced")
        assert row.status == "retired"


def test_a_failing_test_leaves_the_draft(world, monkeypatch):
    monkeypatch.setattr(Q, "run", lambda *a, **k: (_ for _ in ()).throw(InvalidInput("the gateway refused it")))
    with session_scope() as s:
        row = _draft(s, world.user)
        with pytest.raises(InvalidInput, match="gateway refused"):
            defs.test(s, s.merge(world.user), row, row.revision)
        assert row.status == "draft" and row.test_evidence is None


# ------------------------------------------------------------------------------------ MCP
def _principal(tools):
    return G.ClientPrincipal(client_id="mcpc_1", user_id="usr_o",
                             grants={WS: {"role": "analyst", "tools": tools, "quotas": {}}})


def test_grants_name_workspace_tools_or_their_family():
    p = _principal(["query.*", "http.tool.ticket_lookup"])
    assert G.check_grant(p, WS, "query.open_incidents")
    assert G.check_grant(p, WS, "http.tool.ticket_lookup")
    with pytest.raises(Forbidden):
        G.check_grant(p, WS, "http.tool.other")
    with pytest.raises(Forbidden):
        G.check_grant(_principal(["ask"]), WS, "query.open_incidents")
    assert G.min_role("query.x") == "analyst" and not G.is_workspace_tool("query.")


def test_a_draft_query_tool_is_not_listed_or_callable_over_mcp(world, monkeypatch):
    from analystos.mcp import server

    monkeypatch.setattr(server, "_service_user", lambda s, p: s.get(User, "usr_o"))
    monkeypatch.setattr("analystos.capabilities.invoke._snapshot", lambda s, ws: SimpleNamespace(list=lambda kind: []))
    with session_scope() as s:
        row = _draft(s, world.user)
        row = defs.test(s, s.merge(world.user), row, row.revision)
        assert "query.open_incidents" not in server.workspace_tools(s, WS)
    with pytest.raises(PolicyDenied, match="not callable over MCP"):
        server.call_workspace_tool(_principal(["query.*"]), WS, "query.open_incidents", {"priority": "P1", "min_reopens": 0})
    with session_scope() as s:
        row = s.get(Definition, row.id)
        defs.publish(s, s.merge(world.user), row, row.revision)
    with session_scope() as s:
        tools = server.workspace_tools(s, WS)
    assert tools["query.open_incidents"]["schema"]["properties"]["priority"]["enum"] == ["P1", "P2"]
    out = server.call_workspace_tool(_principal(["query.*"]), WS, "query.open_incidents",
                                     {"workspace_id": WS, "priority": "P1", "min_reopens": 0})
    assert out["version"] == 1 and world.runs[-1]["actor"] == "mcp_client:mcpc_1"
    assert world.runs[-1]["purpose"] == "mcp.query_tool:open_incidents"


def _http(status: str, side_effect: str = "read_source") -> CapabilityManifest:
    return CapabilityManifest.model_validate({
        "kind": "Tool", "id": "tool.ticket_lookup", "summary": "Look up a ticket", "entry": "http:ticket_lookup",
        "side_effect": side_effect, "certification": {"status": status},
        "input_schema": {"type": "object", "properties": {"ticket": {"type": "string"}}, "required": ["ticket"]},
        "spec": {"http": {"url": "https://tickets.example.com/lookup", "method": "POST"}}})


@pytest.mark.parametrize("status,side_effect,callable_", [("draft", "read_source", False), ("tested", "read_source", True),
                                                          ("certified", "read_source", True),
                                                          ("certified", "write_external", False),
                                                          ("deprecated", "read_source", False)])
def test_only_a_tested_or_certified_read_only_http_tool_is_callable_over_mcp(world, monkeypatch, status, side_effect,
                                                                               callable_):
    from analystos.mcp import server

    m = _http(status, side_effect)
    snap = SimpleNamespace(list=lambda kind: [m] if kind == "Tool" else [], get=lambda cid: m)
    monkeypatch.setattr("analystos.capabilities.invoke._snapshot", lambda s, ws: snap)
    monkeypatch.setattr("analystos.capabilities.enablement.usable", lambda *a, **k: None)
    monkeypatch.setattr(server, "_service_user", lambda s, p: s.get(User, "usr_o"))
    calls = []
    monkeypatch.setattr("analystos.capabilities.invoke.invoke",
                        lambda user, ws, cid, args, **k: calls.append((cid, args)) or {"status": "ok", "result": {}})
    with session_scope() as s:
        listed = "http.tool.ticket_lookup" in server.workspace_tools(s, WS)
    assert listed is callable_
    if callable_:
        out = server.call_workspace_tool(_principal(["http.*"]), WS, "http.tool.ticket_lookup",
                                         {"workspace_id": WS, "ticket": "T-1"})
        assert out["status"] == "ok" and calls == [("tool.ticket_lookup", {"ticket": "T-1"})]
    else:
        with pytest.raises(PolicyDenied):
            server.call_workspace_tool(_principal(["http.*"]), WS, "http.tool.ticket_lookup", {"ticket": "T-1"})
        assert calls == []
