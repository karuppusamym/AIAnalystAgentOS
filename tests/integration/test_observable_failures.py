"""P4-09 observable failures, through the HTTP API on the compose Postgres: a failed run, a failed schedule and a
failed connector each surface with their cause — on the object itself (run `error`, schedule-run `error`, source
`status`/`last_error`), in a notification where a person must act, and in pilot readiness. Then the source comes back
and the same schedule succeeds. Skips cleanly without the stack."""
from __future__ import annotations

import csv
import shutil
import time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _login(client, email="admin@analystos.local") -> dict[str, str]:
    r = client.post("/api/auth/login", json={"email": email, "password": "ChangeMe123!"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _write(folder, rows=240) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "tickets.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticket_ref", "queue", "reopened", "handle_minutes"])
        for i in range(rows):
            w.writerow([f"T{i:05d}", ["desk", "field", "network"][i % 3], str(i % 7 == 0).lower(), 20 + (i * 37) % 90])


def _wait_run(client, h, ws, run_id, timeout=240) -> dict:
    started = time.time()
    while time.time() - started < timeout:
        run = client.get(f"/api/workspaces/{ws}/analysis/{run_id}", headers=h).json()
        if run.get("status") in ("COMPLETED", "FAILED", "CANCELLED", "REJECTED", "WAITING_USER"):
            return run
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not finish")


def test_failed_run_schedule_and_connector_show_their_cause(control_db, analytics_plane):
    from sqlalchemy import create_engine, text

    from analystos.api.app import app
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id

    settings = get_settings()
    folder_name = f"obs-{new_id('f')[-8:]}"
    folder = settings.upload_dir / folder_name
    _write(folder)
    with TestClient(app) as client:
        h = _login(client)
        ws = client.post("/api/workspaces", headers=h, json={"name": f"Observable failures {folder_name}", "objective":
                                                             "Why are tickets reopened?",
                                                             "policy": {"require_approved_metrics": False}}).json()["id"]
        src = client.post(f"/api/workspaces/{ws}/sources", headers=h,
                          json={"kind": "csv", "name": "tickets share", "config": {"path": folder_name}}).json()["id"]
        assert client.post(f"/api/workspaces/{ws}/sources/{src}/discover", headers=h).status_code == 200
        assert client.put(f"/api/workspaces/{ws}/sources/{src}/selection", headers=h,
                          json={"assets": ["tickets"]}).status_code == 200
        source = next(s for s in client.get(f"/api/workspaces/{ws}/sources", headers=h).json() if s["id"] == src)
        schema = source["staging_schema"]

        # 1. a failed run: the staged snapshot the run reads is gone (purged), so the required steps fail
        with create_engine(settings.analytics_loader_url).begin() as c:
            c.execute(text(f'DROP TABLE "{schema}"."tickets"'))
        made = client.post(f"/api/workspaces/{ws}/analysis", headers=h, json={"objective": "Why are tickets reopened?"})
        assert made.status_code == 200, made.text
        run = _wait_run(client, h, ws, made.json()["id"])
        assert run["status"] == "FAILED", run
        assert run["error"] and "failed" in run["error"], run  # the run says which required step failed
        detail = client.get(f"/api/workspaces/{ws}/analysis/{run['id']}", headers=h).json()
        tasks = detail.get("tasks") or []
        failed = [t for t in tasks if t.get("status") == "FAILED"]
        assert failed and all(t.get("error") for t in failed), tasks  # and each failed step carries its cause

        # 2. a failed schedule: the share is unmounted; the nightly crawl fails and says why
        shutil.rmtree(folder)
        sch = client.post(f"/api/workspaces/{ws}/schedules", headers=h, json={
            "name": "nightly crawl", "kind": "crawl", "cron": "0 6 * * *", "timezone": "UTC", "config": {"source_ids": [src]}})
        assert sch.status_code in (200, 201), sch.text
        fired = client.post(f"/api/schedules/{sch.json()['id']}/run", headers=h).json()
        assert fired["status"] == "failed" and "does not exist" in (fired["error"] or ""), fired
        notes = client.get("/api/notifications", headers=h).json()
        assert any("Schedule failed: nightly crawl" in (n.get("title") or "") and "does not exist" in (n.get("body") or "")
                   for n in notes), notes

        # 3. the failed connector: the source records the cause; discovery answers with it; readiness shows it
        source = next(s for s in client.get(f"/api/workspaces/{ws}/sources", headers=h).json() if s["id"] == src)
        assert source["last_error"] and "does not exist" in source["last_error"], source
        again = client.post(f"/api/workspaces/{ws}/sources/{src}/discover", headers=h)
        assert again.status_code in (400, 404, 422) and "does not exist" in again.text, again.text
        ready = client.get(f"/api/workspaces/{ws}/pilot-readiness", headers=h).json()
        health = next(c for c in ready["checks"] if c["check"] == "connector.health" and src in c["subject"])
        assert ready["verdict"] == "not_ready" and health["status"] == "fail" and "does not exist" in health["reason"]

        # 4. the share comes back: the same schedule succeeds and the error clears
        _write(folder)
        ok = client.post(f"/api/schedules/{sch.json()['id']}/run", headers=h).json()
        assert ok["status"] == "succeeded", ok
        source = next(s for s in client.get(f"/api/workspaces/{ws}/sources", headers=h).json() if s["id"] == src)
        assert source["last_error"] is None
        health = next(c for c in client.get(f"/api/workspaces/{ws}/pilot-readiness", headers=h).json()["checks"]
                      if c["check"] == "connector.health" and src in c["subject"])
        assert health["status"] == "pass"
    shutil.rmtree(folder, ignore_errors=True)
