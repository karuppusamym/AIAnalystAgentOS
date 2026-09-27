"""Demo readiness (2026-09-27): /api/health says plainly which dependency is up, down or off and what that
means, including whether a Temporal worker is actually polling; an unreachable Superset is announced when
publication falls back to the preview destination."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from analystos.api import app as app_module
from analystos.core.config import Settings


def test_every_check_is_labelled_and_down_ones_carry_their_impact():
    checks = {"postgres": {"ok": True}, "superset": {"ok": False, "error": "connection refused"},
              "neo4j": {"ok": True, "enabled": False}, "worker": {"ok": False, "error": "no worker is polling e5-analysis"},
              "sandbox": {"ok": False, "detail": "no namespaces"}}
    problems = app_module.health_problems(checks)
    assert {k: c["state"] for k, c in checks.items()} == {"postgres": "up", "superset": "down", "neo4j": "off",
                                                          "worker": "down", "sandbox": "down"}
    by = {p["dependency"]: p for p in problems}
    assert set(by) == {"superset", "worker", "sandbox"}
    assert by["superset"]["severity"] == "warning" and "preview" in by["superset"]["message"]
    assert by["superset"]["detail"] == "connection refused"
    assert by["worker"]["severity"] == "critical" and "analystos worker" in by["worker"]["message"]
    assert by["sandbox"]["severity"] == "info"  # an optional feature with a fallback is not banner material


@pytest.fixture
def temporal_settings(monkeypatch):
    settings = Settings(_env_file=None, profile="standard", orchestrator="temporal", redis_url="", superset_url="",
                        temporal_queue_prefix="t9")
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: SimpleNamespace(close=lambda: None))
    return settings


def test_health_reports_a_missing_worker_as_critical(temporal_settings, monkeypatch):
    from analystos.workflows import orchestrator

    monkeypatch.setattr(orchestrator, "worker_status", lambda: {"ok": False, "queues": {"t9-analysis": 0}, "missing": ["t9-analysis"]})
    body = TestClient(app_module.app).get("/api/health").json()
    assert body["checks"]["worker"]["state"] == "down" and "t9-analysis" in body["checks"]["worker"]["error"]
    assert body["degraded"] is True
    assert any(p["dependency"] == "worker" and p["severity"] == "critical" for p in body["problems"])
    assert body["checks"]["superset"]["state"] == "off"  # not configured is not an outage


def test_health_reports_a_polling_worker_as_up(temporal_settings, monkeypatch):
    from analystos.workflows import orchestrator

    monkeypatch.setattr(orchestrator, "worker_status", lambda: {"ok": True, "queues": {"t9-analysis": 1}, "missing": []})
    body = TestClient(app_module.app).get("/api/health").json()
    assert body["checks"]["worker"]["state"] == "up" and body["checks"]["worker"]["queues"] == {"t9-analysis": 1}
    assert not any(p["dependency"] == "worker" for p in body["problems"])


def test_local_orchestrator_has_no_worker_to_miss(monkeypatch):
    settings = Settings(_env_file=None, profile="lite")
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    body = TestClient(app_module.app).get("/api/health").json()
    assert body["checks"]["worker"]["state"] == "off" and body["checks"]["temporal"]["state"] == "off"
    assert body["checks"]["superset"]["state"] == "off"


def test_worker_status_counts_only_fresh_pollers(monkeypatch):
    from analystos.workflows import orchestrator

    settings = Settings(_env_file=None, temporal_queue_prefix="t9")
    monkeypatch.setattr(orchestrator, "get_settings", lambda: settings)
    monkeypatch.setattr(orchestrator, "_worker_cache", None)
    ages = {"t9-analysis": [5], "t9-compute": [5, 30], "t9-publish": [600]}  # the publish worker died 10 minutes ago

    class Service:
        async def describe_task_queue(self, req):
            now = time.time()
            return SimpleNamespace(pollers=[SimpleNamespace(last_access_time=SimpleNamespace(ToSeconds=lambda a=a: now - a))
                                            for a in ages[req.task_queue.name]])

    async def client():
        return SimpleNamespace(workflow_service=Service())

    monkeypatch.setattr(orchestrator, "_temporal_client", client)
    st = orchestrator.worker_status(cache_seconds=0)
    assert st["queues"] == {"t9-analysis": 1, "t9-compute": 2, "t9-publish": 0}
    assert st["ok"] is False and st["missing"] == ["t9-publish"]


def test_unreachable_superset_is_announced_when_publication_falls_back_to_preview(monkeypatch):
    from analystos.agents import publisher

    said = []
    ctx = SimpleNamespace(policy=SimpleNamespace(publish_destinations=["superset"]),
                          say=lambda text, kind=None: said.append((kind, text)))
    monkeypatch.setattr("analystos.services.platform_settings.get",
                        lambda: SimpleNamespace(features=SimpleNamespace(superset_publishing=True)))
    monkeypatch.setattr(publisher, "get_settings", lambda: SimpleNamespace(superset_url="http://superset:8088"))
    # test_connection reports an outage by returning ok=False, not by raising
    monkeypatch.setattr("analystos.publishing.base.get_publisher", lambda dest, settings=None: SimpleNamespace(
        test_connection=lambda: {"ok": False, "error": "Superset login failed: connection refused"}))
    assert publisher.choose_destination(ctx) == "preview"
    assert said and said[0][0] == "decision" and "Superset not reachable" in said[0][1] and "connection refused" in said[0][1]
