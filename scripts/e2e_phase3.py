"""Live proof of Phase 3 (scheduled & continuous analytics) through the HTTP API.

    python scripts/e2e_phase3.py [--workspace ws_...] [--out docs/60-delivery/evidence]

Uses the workspace of the most recent MVP evidence file unless --workspace is given (it needs a
completed run with a dataset and KPIs). Exercises: scheduled re-analysis with a diff against the
previous run and a generated report; report downloads; metric/data-quality monitors with JEV
triage; alert -> automatic investigation; notifications. Writes e2e-phase3-<ts>.md/.json.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

PASSWORD = os.getenv("ANALYSTOS_DEMO_PASSWORD", "ChangeMe123!")


class Api:
    def __init__(self, base: str, email: str):
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=600)
        r = self.c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        r.raise_for_status()
        self.c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def get(self, path, **kw):
        r = self.c.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None):
        r = self.c.post(path, json=body or {})
        if r.status_code >= 400:
            raise SystemExit(f"POST {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()


def wait_run(api: Api, ws: str, run: str, timeout=1800) -> dict:
    started = time.time()
    while time.time() - started < timeout:
        d = api.get(f"/api/workspaces/{ws}/analysis/{run}")
        if d["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return d
        time.sleep(4)
    raise SystemExit(f"timeout waiting for run {run}")


def run_seconds(detail: dict | None) -> float | None:
    """Wall-clock duration of an analysis run from its own timestamps (None while it has not finished)."""
    if not detail or not detail.get("started_at") or not detail.get("finished_at"):
        return None
    return round((datetime.fromisoformat(detail["finished_at"]) - datetime.fromisoformat(detail["started_at"])).total_seconds(), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.getenv("ANALYSTOS_API", "http://localhost:8000"))
    ap.add_argument("--workspace")
    ap.add_argument("--out", default="docs/60-delivery/evidence")
    args = ap.parse_args()
    started = datetime.now(UTC)
    ws = args.workspace
    if not ws:  # the workspace of the latest MVP evidence report
        latest = sorted(glob.glob(os.path.join(args.out, "e2e-2*.md")))[-1]
        ws = Path(latest).read_text().split("(workspace `", 1)[1].split("`", 1)[0]
    analyst = Api(args.api, "analyst@analystos.local")
    ev: dict = {"api": args.api, "workspace_id": ws, "started_at": started.isoformat(), "checks": {}}
    check = ev["checks"]
    runs = analyst.get(f"/api/workspaces/{ws}/analysis")
    baseline = next(r for r in runs if r["status"] == "COMPLETED" and (r.get("origin") or {}).get("type", "user") in ("user", "schedule"))
    print("workspace", ws, "baseline run", baseline["id"])

    # 1. Scheduled re-analysis with diff + report (run-now uses the same path as a cron firing)
    sch = analyst.post(f"/api/workspaces/{ws}/schedules", {
        "name": "Weekly incident review", "kind": "reanalysis", "cron": "58 6 * * 1", "timezone": "Europe/London",
        "config": {"baseline_run_id": baseline["id"], "refresh_first": True, "publish": "skip",
                   "report": {"kind": "weekly_summary", "formats": ["html", "pdf", "xlsx"]}}})
    srun = analyst.post(f"/api/schedules/{sch['id']}/run")
    rerun_id = srun["result"]["run_id"]
    print("scheduled re-analysis run", rerun_id)
    rerun = wait_run(analyst, ws, rerun_id)
    schedules = analyst.get(f"/api/workspaces/{ws}/schedules")
    srun_now = next(r for s in schedules if s["id"] == sch["id"] for r in s["recent_runs"] if r["id"] == srun["id"])
    changes = (rerun.get("summary") or {}).get("changes") or {}
    check["schedule_created_with_next_fire"] = bool(sch["next_run_at"])
    check["scheduled_reanalysis_completed"] = rerun["status"] == "COMPLETED" and srun_now["status"] == "succeeded"
    check["scheduled_run_did_not_publish"] = all(t["status"] == "SKIPPED" for t in rerun["tasks"] if t["key"] in ("publish_request", "publish"))
    check["diff_against_previous_run"] = changes.get("previous_run_id") == baseline["id"] and \
        sum(len(changes.get(k, [])) for k in ("new", "persisting", "changed", "resolved")) >= 1
    # Same data: every carried-forward claim is re-tested; nothing silently drops out of the comparison.
    check["previous_findings_all_retested"] = changes.get("not_retested") == []
    report_id = (rerun.get("summary") or {}).get("report_artifact_id")
    downloads = {}
    for fmt, magic in (("pdf", b"%PDF"), ("xlsx", b"PK"), ("html", b"<!")):
        r = analyst.c.get(f"/api/artifacts/{report_id}/download", params={"format": fmt})
        downloads[fmt] = {"status": r.status_code, "bytes": len(r.content), "ok": r.status_code == 200 and r.content[:len(magic)].upper() == magic.upper()}
    check["weekly_report_generated_and_downloadable"] = bool(report_id) and all(d["ok"] for d in downloads.values())
    ev["reanalysis"] = {"schedule": sch, "schedule_run": srun_now, "run_id": rerun_id, "changes": {k: [c.get("title") for c in changes.get(k, [])] for k in ("new", "persisting", "changed", "resolved", "not_retested")},
                        "metric_deltas": changes.get("metrics"), "report_artifact_id": report_id, "downloads": downloads,
                        "cost_usd": rerun.get("cost_usd")}

    # 2. Monitors + monitor schedule
    metric_names = {a["name"] for a in analyst.get(f"/api/workspaces/{ws}/artifacts", params={"type": "metric"})}
    rate = next((n for n in sorted(metric_names) if "breach" in n or n.endswith("_rate")), "record_count")
    mons = [
        analyst.post(f"/api/workspaces/{ws}/monitors", {"name": "Weekly volume drift", "kind": "metric_drift",
                                                        "config": {"metric": "record_count", "grain": "week", "lookback": 8, "z_threshold": 3}}),
        analyst.post(f"/api/workspaces/{ws}/monitors", {"name": "Volume regime change", "kind": "change_point",
                                                        "config": {"metric": "record_count", "grain": "week", "recent_periods": 60}}),
        analyst.post(f"/api/workspaces/{ws}/monitors", {"name": f"{rate} above target", "kind": "metric_threshold", "auto_investigate": True,
                                                        "config": {"metric": rate, "grain": "month", "op": ">", "value": 0.0}}),
        analyst.post(f"/api/workspaces/{ws}/monitors", {"name": "Incident data quality", "kind": "data_quality", "config": {}}),
    ]
    msch = analyst.post(f"/api/workspaces/{ws}/schedules", {"name": "Hourly monitors", "kind": "monitor", "cron": "5 * * * *",
                                                           "config": {"monitor_ids": [m["id"] for m in mons]}})
    mrun = analyst.post(f"/api/schedules/{msch['id']}/run")
    results = mrun["result"].get("monitors", {})
    alerts = analyst.get(f"/api/workspaces/{ws}/alerts")
    threshold_alert = next((a for a in alerts if a["monitor_id"] == mons[2]["id"]), None)
    check["monitor_schedule_evaluated_all"] = mrun["status"] == "succeeded" and len(results) == len(mons) and not any("error" in r for r in results.values())
    check["threshold_alert_raised"] = threshold_alert is not None
    check["alert_triaged_by_jev"] = bool(threshold_alert and (threshold_alert["data"].get("triage") or {}).get("model", "").startswith("typesafe/jev"))
    check["data_quality_baseline_recorded"] = bool(next(m for m in analyst.get(f"/api/workspaces/{ws}/monitors") if m["id"] == mons[3]["id"])["last_result"].get("issues") is not None)
    series = analyst.get(f"/api/monitors/{mons[0]['id']}/series")
    check["metric_series_governed"] = len(series["points"]) >= 20
    inv_id = threshold_alert and threshold_alert.get("investigation_run_id")
    inv = wait_run(analyst, ws, inv_id) if inv_id else None
    check["auto_investigation_completed"] = bool(inv and inv["status"] == "COMPLETED" and inv["origin"]["type"] == "alert")
    ev["monitoring"] = {"monitors": [{"name": m["name"], "kind": m["kind"], "result": results.get(m["id"])} for m in mons],
                        "alerts": [{"severity": a["severity"], "title": a["title"], "message": a["message"], "triage": a["data"].get("triage"),
                                    "investigation_run_id": a.get("investigation_run_id")} for a in alerts],
                        "series_points": len(series["points"]), "investigation": {"run_id": inv_id, "status": inv and inv["status"],
                                                                                  "verified": inv and len([i for i in inv["insights"] if i["status"] == "verified"]),
                                                                                  "objective": inv and inv["objective"]}}

    # 3. Notifications
    notes = analyst.get("/api/notifications")
    kinds = {n["kind"] for n in notes if n["workspace_id"] == ws}
    check["notifications_for_report_and_alert"] = {"report", "alert"} <= kinds
    ev["notifications"] = [{"kind": n["kind"], "title": n["title"]} for n in notes if n["workspace_id"] == ws][:15]

    finished = datetime.now(UTC)
    ev["finished_at"] = finished.isoformat()
    ev["duration"] = {"scenario_seconds": round((finished - started).total_seconds(), 1),
                      "reanalysis_run_seconds": run_seconds(rerun), "investigation_run_seconds": run_seconds(inv)}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    (out / f"e2e-phase3-{stamp}.json").write_text(json.dumps(ev, indent=2, default=str))
    passed = sum(1 for v in check.values() if v)
    lines = [f"# Phase 3 end-to-end evidence — {started:%Y-%m-%d %H:%M} UTC", "",
             f"Live run through `{args.api}` in workspace `{ws}` (baseline run `{baseline['id']}`). Raw: `e2e-phase3-{stamp}.json`.", "",
             f"**{passed}/{len(check)} checks passed.**", "",
             f"Duration: scenario {ev['duration']['scenario_seconds']} s (started {started:%H:%M:%S}, finished {finished:%H:%M:%S} UTC); "
             f"scheduled re-analysis run {ev['duration']['reanalysis_run_seconds']} s; "
             f"automatic investigation run {ev['duration']['investigation_run_seconds']} s.",
             "", "| Check | Result |", "|---|---|"]
    lines += [f"| {k} | {'PASS' if v else '**FAIL**'} |" for k, v in check.items()]
    lines += ["", "## Scheduled re-analysis", "", f"Run `{rerun_id}`; schedule run `{srun['id']}` ({srun_now['status']}).", ""]
    for k in ("new", "persisting", "changed", "resolved", "not_retested"):
        lines.append(f"- **{k}**: " + ("; ".join(ev["reanalysis"]["changes"][k]) or "—"))
    lines += ["", "KPI deltas:", "", "| KPI | Value | Previous | Change |", "|---|---|---|---|"]
    lines += [f"| {m['name']} | {m['value']} | {m['previous_value']} | {m['pct_change']} |" for m in (changes.get("metrics") or [])]
    lines += ["", f"Report `{report_id}` downloads: " + ", ".join(f"{k} {v['bytes']} B" for k, v in downloads.items()), "",
              "## Monitoring", "", "| Monitor | Kind | Alert | Message |", "|---|---|---|---|"]
    lines += [f"| {m['name']} | {m['kind']} | {(m['result'] or {}).get('alert')} | {((m['result'] or {}).get('message') or '')[:160]} |" for m in ev["monitoring"]["monitors"]]
    lines += ["", "Alerts:", ""] + [f"- [{a['severity']}] {a['title']} — {a['message'][:200]} (triage: {a['triage']})" for a in ev["monitoring"]["alerts"]]
    inv_ev = ev["monitoring"]["investigation"]
    lines += ["", f"Automatic investigation `{inv_ev['run_id']}`: {inv_ev['status']}, {inv_ev['verified']} verified findings.", "",
              "## Notifications", ""] + [f"- {n['kind']}: {n['title']}" for n in ev["notifications"]]
    (out / f"e2e-phase3-{stamp}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[: len(check) + 9]))
    print(f"wrote {out / f'e2e-phase3-{stamp}.md'}")
    return 0 if passed == len(check) else 1


if __name__ == "__main__":
    sys.exit(main())
