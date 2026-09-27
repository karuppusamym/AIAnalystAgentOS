"""`analystos demo-seed` is idempotent: each piece is looked up first and only what is missing is made
(the live proof against a running stack: docs/60-delivery/evidence/2026-09-27-demo-readiness.md)."""
from __future__ import annotations

from typing import Any

import pytest

from analystos.demo import seed


class FakeApi:
    """Answers GETs from a route table and records every write."""

    def __init__(self, routes: dict[str, Any]):
        self.routes, self.writes = routes, []

    def get(self, path: str, **kw: Any) -> Any:
        value = self.routes[path]
        return value() if callable(value) else value

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, body)

    def call(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        self.writes.append((method, path, body))
        handler = self.routes.get((method, path))
        return handler(body) if callable(handler) else handler or {}


def test_an_existing_workspace_and_its_members_are_reused():
    api = FakeApi({"/api/workspaces": [{"id": "ws_1", "name": seed.demo()["workspace"]["name"]}],
                   "/api/workspaces/ws_1": {"members": [{"email": "analyst@analystos.local", "role": "editor"},
                                                        {"email": "approver@analystos.local", "role": "approver"}]}})
    assert seed.ensure_workspace(api) == "ws_1"
    assert api.writes == []


def test_a_missing_workspace_is_created_with_its_members():
    api = FakeApi({"/api/workspaces": [], "/api/workspaces/ws_9": {"members": []},
                   ("POST", "/api/workspaces"): lambda body: {"id": "ws_9", **body}})
    assert seed.ensure_workspace(api) == "ws_9"
    assert [w[1] for w in api.writes] == ["/api/workspaces", "/api/workspaces/ws_9/members", "/api/workspaces/ws_9/members"]
    assert api.writes[0][2]["policy"] == {"require_approved_metrics": False}


def _assertion(a: dict, state: str = "reviewed", subject: str | None = None) -> dict:
    key = f"{a['group']}.{a['field']}" + (f":{subject}" if subject else "")
    return {**a, "key": key, "review_state": state}


GRAIN = {"group": "data_semantics", "field": "grain", "value": "one row per x"}


def test_the_brief_only_sends_the_assertions_it_lacks():
    reviewed_grains = [_assertion(GRAIN, subject=f"src_1.{t}") for t in seed.demo()["source"]["select"]]
    have = [_assertion(a) for a in seed.demo()["brief"][1:]] + reviewed_grains
    after = {"version": 5, "assertions": have + [_assertion(seed.demo()["brief"][0])]}
    api = FakeApi({"/api/workspaces/ws_1/brief": {"version": 4, "assertions": have},
                   ("PATCH", "/api/workspaces/ws_1/brief"): after})
    seed.ensure_brief(api, "ws_1")
    (method, path, body), = api.writes
    assert (method, path) == ("PATCH", "/api/workspaces/ws_1/brief")
    assert [op["assertion"]["field"] for op in body["ops"]] == [seed.demo()["brief"][0]["field"]]
    api = FakeApi({"/api/workspaces/ws_1/brief": after})
    seed.ensure_brief(api, "ws_1")
    assert api.writes == []


def test_inferred_grains_of_the_investigated_tables_are_reviewed_and_the_file_stays_a_suggestion():
    have = [_assertion(a) for a in seed.demo()["brief"]]
    inferred = {"version": 7, "assertions": have + [_assertion(GRAIN, "suggested", f"src_1.{t}") for t in seed.demo()["source"]["select"]]
                + [_assertion(GRAIN, "suggested", "src_2.team_targets")]}
    api = FakeApi({"/api/workspaces/ws_1/brief": {"version": 6, "assertions": have},
                   ("POST", "/api/workspaces/ws_1/brief/suggestions"): inferred,
                   ("PATCH", "/api/workspaces/ws_1/brief"): {"version": 8, "assertions": []}})
    seed.ensure_brief(api, "ws_1")
    assert [w[:2] for w in api.writes] == [("POST", "/api/workspaces/ws_1/brief/suggestions"), ("PATCH", "/api/workspaces/ws_1/brief")]
    reviewed = [op["key"] for op in api.writes[1][2]["ops"]]
    assert reviewed == [f"data_semantics.grain:src_1.{t}" for t in seed.demo()["source"]["select"]]


def test_a_completed_verified_investigation_is_not_run_again():
    api = FakeApi({"/api/workspaces/ws_1/analysis": [{"id": "run_1", "status": "COMPLETED", "origin": {"type": "user"}}],
                   "/api/workspaces/ws_1/analysis/run_1": {"insights": [{"status": "verified"}]}})
    assert seed.ensure_investigation(api, api, "ws_1", timeout=1) == "run_1"
    assert api.writes == []


def test_a_new_investigation_is_scoped_to_servicenow_and_its_publication_approved(monkeypatch):
    monkeypatch.setattr(seed.time, "sleep", lambda s: None)
    states = iter([
        {"status": "WAITING_USER", "insights": [], "approvals": [{"id": "apr_1", "status": "pending", "action": "publish_dashboard"}]},
        {"status": "COMPLETED", "insights": [{"status": "verified"}], "approvals": [], "summary": {}},
    ])
    analyst = FakeApi({"/api/workspaces/ws_1/analysis": [],
                       "/api/workspaces/ws_1/sources": [{"id": "src_sn", "kind": "servicenow"}, {"id": "src_csv", "kind": "csv"}],
                       ("POST", "/api/workspaces/ws_1/analysis"): {"id": "run_2", "status": "NEW"},
                       "/api/workspaces/ws_1/analysis/run_2": lambda: next(states)})
    approver = FakeApi({})
    assert seed.ensure_investigation(analyst, approver, "ws_1", timeout=60) == "run_2"
    assert analyst.writes[0][2]["source_ids"] == ["src_sn"]
    assert approver.writes == [("POST", "/api/approvals/apr_1/approve", {"reason": "demo seed"})]


def test_a_failed_investigation_stops_the_seed_with_its_error(monkeypatch):
    monkeypatch.setattr(seed.time, "sleep", lambda s: None)
    analyst = FakeApi({"/api/workspaces/ws_1/analysis": [{"id": "run_3", "status": "RUNNING", "origin": {"type": "user"}}],
                       "/api/workspaces/ws_1/analysis/run_3": {"status": "FAILED", "error": "gateway refused", "insights": [],
                                                                "approvals": []}})
    with pytest.raises(SystemExit, match="gateway refused"):
        seed.ensure_investigation(analyst, analyst, "ws_1", timeout=60)


def test_the_cli_routes_demo_seed(monkeypatch):
    from analystos import cli

    seen = {}
    monkeypatch.setattr(seed, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    cli.main(["demo-seed", "--api", "http://x"])
    assert seen["argv"] == ["--api", "http://x"]


def test_the_pack_demo_file_is_complete_and_its_recipe_validates():
    import copy

    from analystos.contracts.recipe import validate_recipe

    d = seed.demo()
    assert {"workspace", "source", "brief", "file", "recipe", "schedule", "monitor"} <= set(d)
    assert set(d["source"]["select"]) <= set(d["source"]["tables"])
    spec = copy.deepcopy(d["recipe"])
    header = d["file"]["content"].splitlines()[0].split(",")
    types = {"sla_target_hours": "bigint", "headcount": "bigint"}
    spec["nodes"].insert(0, {"op": "source", "id": "targets", "asset": "src_x.team_targets",
                             "schema": [{"name": c, "type": types.get(c, "text")} for c in header]})
    assert validate_recipe(spec).hash


# ------------------------------------------------------------------------------ process mining workspace
def test_the_process_demo_file_names_a_second_workspace_over_the_activity_log():
    from analystos.skills.process_mining import declared_event_logs, declared_for

    d = seed.process_demo()
    assert d["workspace"]["name"] != seed.demo()["workspace"]["name"]
    assert set(d["source"]["select"]) <= set(d["source"]["tables"])
    assert d["process"]["table"] in d["source"]["select"]
    model = declared_for(declared_event_logs(), [d["process"]["table"]])
    assert {a["segment"] for a in d["process"]["analyses"]} <= set(model["segments"])


CANDIDATE = {"asset_id": "ast_1", "name": "u_task_activity", "segments": [{"column": "task_type", "values": []}],
             "mapping": {"case_column": "task_sys_id", "activity_column": "activity", "timestamp_column": "activity_at",
                         "resource_column": "assignment_group"}}
SUMMARY = {"summary": {"cases": 10, "variants": 2, "fitness": 0.8}}


def test_missing_process_analyses_are_saved_with_the_detected_mapping():
    d = seed.process_demo()
    first = d["process"]["analyses"][0]
    api = FakeApi({"/api/workspaces/ws_p/process/analyses": [{"name": first["name"]}],
                   "/api/workspaces/ws_p/process/candidates": {"candidates": [CANDIDATE]},
                   ("POST", "/api/workspaces/ws_p/process/analyze"): SUMMARY})
    seed.ensure_process_analyses(api, "ws_p")
    bodies = [w[2] for w in api.writes]
    assert [b["name"] for b in bodies] == [a["name"] for a in d["process"]["analyses"][1:]]
    assert bodies[0] == {"asset_id": "ast_1", **CANDIDATE["mapping"], "save": True, "name": bodies[0]["name"],
                         "filters": [{"column": "task_type", "op": "=", "value": d["process"]["analyses"][1]["segment"]}]}


def test_saved_process_analyses_are_not_made_again_and_a_missing_log_stops_the_seed():
    names = [{"name": a["name"]} for a in seed.process_demo()["process"]["analyses"]]
    api = FakeApi({"/api/workspaces/ws_p/process/analyses": names})
    seed.ensure_process_analyses(api, "ws_p")
    assert api.writes == []
    api = FakeApi({"/api/workspaces/ws_p/process/analyses": [], "/api/workspaces/ws_p/process/candidates": {"candidates": []}})
    with pytest.raises(SystemExit, match="not detected as an event log"):
        seed.ensure_process_analyses(api, "ws_p")


def test_only_process_builds_just_the_process_workspace(monkeypatch):
    built = []
    monkeypatch.setattr(seed, "wait_for_api", lambda base, seconds: {"checks": {}})
    monkeypatch.setattr(seed, "Api", lambda base, email, password: email)
    monkeypatch.setattr(seed, "ensure_process_workspace", lambda admin, analyst, url: built.append("process") or "ws_p")
    monkeypatch.setattr(seed, "ensure_workspace", lambda admin: built.append("investigation") or "ws_i")
    assert seed.main(["--only", "process"]) == 0
    assert built == ["process"]
