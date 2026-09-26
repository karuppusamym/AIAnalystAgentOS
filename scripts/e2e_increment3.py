"""Live proof of increment 3 through the HTTP API: any database, metadata crawler, token economy, admin control.

    python scripts/e2e_increment3.py [--out docs/60-delivery/evidence]

Builds a seeded retail SQLite database with planted effects (not ServiceNow), uploads it, registers it as
a `sqlite` source, crawls it (full, incremental, drift), curates metadata, explains SQL, applies the
`token_saver` preset and runs a governed analysis on it, then checks the token-savings ledger, a
forecast-deviation monitor and a scheduled crawl. Restores the previous platform settings at the end.
Writes e2e-increment3-<ts>.md/.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

PASSWORD = os.getenv("ANALYSTOS_DEMO_PASSWORD", "ChangeMe123!")
OBJECTIVE = ("Understand what drives product returns, order value and shipping delays in our online retail orders, "
             "and which channels, regions and customer segments need attention.")


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

    def call(self, method: str, path: str, body=None, expect: bool = False):
        r = self.c.request(method, path, json=body if body is not None else {})
        if expect:
            return r.status_code, (r.json() if r.content else {})
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        return r.json()

    def post(self, path, body=None, expect=False):
        return self.call("POST", path, body, expect)

    def put(self, path, body=None):
        return self.call("PUT", path, body)

    def patch(self, path, body=None):
        return self.call("PATCH", path, body)

    def upload(self, path: str, file: Path):
        with file.open("rb") as fh:
            r = self.c.post(path, files={"file": (file.name, fh, "application/octet-stream")})
        r.raise_for_status()
        return r.json()


# ------------------------------------------------------------------------------------ data
def _load_generator():
    """The retail benchmark lives in the sales domain pack (packs/sales/benchmark/generator.py)."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "packs" / "sales" / "benchmark" / "generator.py"
    spec = importlib.util.spec_from_file_location("sales_benchmark_generator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_shop_db = _load_generator().build_shop_db


def wait_run(api: Api, ws: str, run: str, predicate, timeout=2400) -> dict:
    started = time.time()
    while time.time() - started < timeout:
        d = api.get(f"/api/workspaces/{ws}/analysis/{run}")
        if predicate(d):
            return d
        if d["status"] in ("FAILED", "CANCELLED"):
            raise SystemExit(f"run ended {d['status']}: {d.get('error')}")
        time.sleep(4)
    raise SystemExit(f"timeout waiting for run {run}")


def wait_crawl(api: Api, crawl_id: str, timeout=600) -> dict:
    started = time.time()
    while time.time() - started < timeout:
        c = api.get(f"/api/crawls/{crawl_id}")
        if c["status"] != "running":
            return c
        time.sleep(1)
    raise SystemExit(f"timeout waiting for crawl {crawl_id}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.getenv("ANALYSTOS_API", "http://localhost:8000"))
    ap.add_argument("--out", default="docs/60-delivery/evidence")
    args = ap.parse_args()
    started = datetime.now(UTC)
    ev: dict = {"api": args.api, "started_at": started.isoformat(), "checks": {}}
    check = ev["checks"]
    admin, analyst, approver = (Api(args.api, f"{u}@analystos.local") for u in ("admin", "analyst", "approver"))

    # 1. source-kind catalog
    kinds = analyst.get("/api/source-kinds")
    check["1_any_database_kind_catalog"] = len(kinds) >= 12 and all(
        k["driver_installed"] for k in kinds if k["kind"] in ("postgres", "mysql", "sqlite", "duckdb"))
    ev["kinds"] = [{k2: k[k2] for k2 in ("kind", "category", "execution_mode", "driver_installed")} for k in kinds]

    # 2. admin control plane: remember the current version, apply the token_saver preset
    before = admin.get("/api/admin/settings")
    base_version = before.get("version", 0)
    admin.post("/api/admin/settings/preset", {"preset": "token_saver", "note": "increment-3 evidence"})
    models = admin.get("/api/admin/models")
    routing = models["effective"]
    check["2_admin_preset_applied_at_runtime"] = routing.get("hypothesis_generation", {}).get("mode") == "auto" and \
        routing.get("verification", {}).get("mode") == "always"
    ev["settings"] = {"base_version": base_version, "modes": {p: r.get("mode") for p, r in routing.items()}}

    demo_items: list[str] = []
    try:
        # 3. workspace + an uploaded SQLite database registered as a source
        ws = admin.post("/api/workspaces", {"name": f"Retail orders (SQLite) {started:%Y%m%d-%H%M}", "objective": OBJECTIVE,
                                            "description": "Increment 3 evidence: any database", "autonomy_level": 3,
                                            # predates the P4-K03 approved-metric gate (tests/integration/test_semantic_layer.py)
                                            "policy": {"require_approved_metrics": False}})
        wid = ws["id"]
        admin.post(f"/api/workspaces/{wid}/members", {"email": "analyst@analystos.local", "role": "editor"})
        admin.post(f"/api/workspaces/{wid}/members", {"email": "approver@analystos.local", "role": "approver"})
        tmp = Path(tempfile.mkdtemp()) / "shop.db"
        ev["data"] = build_shop_db(tmp)
        up = analyst.upload(f"/api/workspaces/{wid}/uploads", tmp)
        src = analyst.post(f"/api/workspaces/{wid}/sources", {"kind": "sqlite", "name": "Retail shop (SQLite)",
                                                              "config": {"path": f"{wid}/shop.db"}, "secret_ref": None})
        check["3_sqlite_source_registered"] = src["kind"] == "sqlite" and src["execution_mode"] == "staged" and up["bytes"] > 0

        # 4. discovery = full crawl with deterministic semantics and PII
        disc = analyst.post(f"/api/workspaces/{wid}/sources/{src['id']}/discover")
        cat = {a["name"]: a for a in analyst.get(f"/api/workspaces/{wid}/catalog")}
        cust_cols = {c["name"]: c for c in cat["customer"]["columns"]}
        check["4_crawl_semantics_and_pii"] = disc["stats"]["new"] == 4 and cat["orders"]["role"] in ("fact", "event") and \
            "pii" in cust_cols["email"]["tags"] and "pii" in cust_cols["customer_name"]["tags"] and disc["stats"]["model_calls"] == 0
        ev["crawl_full"] = {"stats": disc["stats"], "semantics": {n: {k: a[k] for k in ("role", "domain", "grain", "confidence",
                                                                                         "description_origin")} for n, a in cat.items()},
                            "pii": {f"{n}.{c['name']}": c["pii"]["category"] for n, a in cat.items() for c in a["columns"] if c.get("pii")}}

        # 5. select + stage, then an incremental crawl with governed profiling
        sel = analyst.put(f"/api/workspaces/{wid}/sources/{src['id']}/selection", {"assets": ["orders", "customer", "product", "region"]})
        c1 = wait_crawl(analyst, analyst.post(f"/api/workspaces/{wid}/sources/{src['id']}/crawl",
                                              {"mode": "incremental", "profile": True})["id"])
        check["5_incremental_crawl_skips_unchanged"] = c1["status"] == "succeeded" and c1["stats"]["unchanged"] == 4 and \
            c1["stats"]["touched"] == 0 and sum(x["row_count"] for x in sel["loaded"]) > 25000
        ev["crawl_incremental"] = {"stats": c1["stats"], "loaded": sel["loaded"]}

        # 6. curation wins, then schema drift is detected precisely
        region_id = cat["region"]["id"]
        analyst.patch(f"/api/assets/{region_id}/metadata", {"description": "Sales regions used for regional reporting.", "reviewed": True})
        build_shop_db(tmp, drift=True)
        analyst.upload(f"/api/workspaces/{wid}/uploads", tmp)
        c2 = wait_crawl(analyst, analyst.post(f"/api/workspaces/{wid}/sources/{src['id']}/crawl", {"mode": "full", "profile": True})["id"])
        changed = {c["key"].rsplit(".", 1)[-1]: c for c in c2["changes"]["changed"]}
        cat2 = {a["name"]: a for a in analyst.get(f"/api/workspaces/{wid}/catalog")}
        notes = [n for n in analyst.get("/api/notifications") if n["workspace_id"] == wid and n["kind"] == "schema_change"]
        check["6_schema_drift_detected_and_curation_kept"] = set(changed) == {"orders"} and changed["orders"]["added"] == ["coupon_code"] \
            and c2["changes"]["deprecated"] == [] and cat2["region"]["description_origin"] == "user" and cat2["region"]["reviewed"] \
            and bool(notes)
        ev["crawl_drift"] = {"stats": c2["stats"], "changes": c2["changes"], "notification": notes[0]["title"] if notes else None}

        # 7. deterministic SQL explanation + gateway verdict (no execution)
        schema = cat2["orders"]["fq"].split(".")[0]
        ok = analyst.post(f"/api/workspaces/{wid}/query/explain", {"sql": f"SELECT channel, AVG(net_amount) AS avg_value FROM {schema}.orders "
                                                                          "WHERE returned GROUP BY channel ORDER BY avg_value DESC"})
        bad = analyst.post(f"/api/workspaces/{wid}/query/explain", {"sql": f"SELECT email FROM {schema}.customer"})
        check["7_sql_explained_without_model"] = ok["gateway"]["accepted"] and "grouped by channel" in ok["summary"] and \
            not bad["gateway"]["accepted"]
        ev["explain"] = {"accepted": ok["summary"], "rejected": bad["gateway"]}

        # 8. governed analysis on the generic database under the token_saver preset
        run = analyst.post(f"/api/workspaces/{wid}/analysis", {"objective": OBJECTIVE})
        rid = run["id"]
        print("run", rid, flush=True)
        detail = wait_run(analyst, wid, rid, lambda d: d["status"] in ("WAITING_USER", "PAUSED", "COMPLETED") and
                          any(t["key"] == "publish_request" and t["status"] == "COMPLETED" for t in d["tasks"]) or d["status"] == "COMPLETED")
        approvals = analyst.get(f"/api/workspaces/{wid}/approvals")
        pending = [a for a in approvals if a["status"] == "pending"]
        if pending:
            approver.post(f"/api/approvals/{pending[0]['id']}/approve", {"reason": "increment 3 evidence"})
        detail = wait_run(analyst, wid, rid, lambda d: d["status"] == "COMPLETED")
        verified = [i for i in detail["insights"] if i["status"] == "verified"]
        titles = " | ".join(i["title"].lower() for i in verified)
        planted = {"returns_by_channel": "marketplace" in titles or ("return" in titles and "channel" in titles),
                   "value_by_segment": "enterprise" in titles or ("segment" in titles and ("amount" in titles or "value" in titles)),
                   "shipping_by_region": "apac" in titles or ("shipping" in titles and "region" in titles)}
        check["8_analysis_on_generic_database_completed"] = detail["status"] == "COMPLETED" and len(verified) >= 2
        check["9_planted_effects_found"] = sum(planted.values()) >= 2
        check["10_no_false_payment_method_finding"] = "payment" not in titles
        console = analyst.get(f"/api/workspaces/{wid}/analysis/{rid}/console")
        calls = console.get("model_calls", [])
        skipped = [c for c in calls if c.get("status") == "skipped"]
        ev["analysis"] = {"run_id": rid, "verified": [i["title"] for i in verified], "planted": planted,
                          "cost_usd": detail.get("cost_usd"), "tokens": detail.get("tokens"),
                          "model_calls": console["cost"]["model_calls"], "cache_hits": console["cost"]["cache_hits"],
                          "tokens_saved": console["cost"]["tokens_saved"],
                          "deterministic_skips": [(c["purpose"], c.get("error")) for c in skipped],
                          "publication": (detail.get("summary") or {}).get("publication")}

        # 9. token economy ledger
        sav = admin.get("/api/admin/token-savings", params={"days": 1})
        tot = sav.get("totals", sav)
        check["11_tokens_saved_accounted"] = (tot.get("tokens_saved") or 0) > 0 and (tot.get("deterministic_skips") or 0) >= 1 and len(skipped) >= 1
        ev["token_savings"] = sav

        # 10. forecast-deviation monitor + scheduled crawl
        # Demo items are marked ("[demo] ", config.demo) and disabled in `finally` below.
        mon = analyst.post(f"/api/workspaces/{wid}/monitors", {"name": "[demo] Weekly orders vs forecast",
                                                              "kind": "forecast_deviation",
                                                              "config": {"metric": "record_count", "grain": "week", "z": 2.5,
                                                                         "demo": True}})
        demo_items.append(f"/api/monitors/{mon['id']}")
        mres = analyst.post(f"/api/monitors/{mon['id']}/evaluate")
        check["12_forecast_monitor_evaluated"] = "expected" in (mres.get("result") or mres) or "expected" in json.dumps(mres)
        sch = analyst.post(f"/api/workspaces/{wid}/schedules", {"name": "[demo] Nightly catalog crawl", "kind": "crawl",
                                                               "cron": "17 2 * * *", "timezone": "UTC",
                                                               "config": {"mode": "incremental", "demo": True}})
        demo_items.append(f"/api/schedules/{sch['id']}")
        srun = analyst.post(f"/api/schedules/{sch['id']}/run")
        check["13_scheduled_crawl_succeeded"] = srun["status"] == "succeeded" and bool(srun["result"]["crawls"])
        ev["monitor"], ev["scheduled_crawl"] = mres, srun
    finally:
        # 11. demo schedules/monitors off (they would keep firing, and calling models, on this stack)
        for path in demo_items:
            status, _ = analyst.call("PATCH", path, {"enabled": False}, expect=True)
            print(f"disabled demo item {path} (HTTP {status}); leftovers: `analystos schedules disable-demo`")
        # 12. restore the platform settings that were in force before the evidence run ("balanced" is the
        # default; max_quality would leave every purpose on the large model tier)
        hist = admin.get("/api/admin/settings/history")
        if base_version:
            admin.post("/api/admin/settings/rollback", {"version": base_version, "note": "restore after increment-3 evidence"})
        else:
            admin.post("/api/admin/settings/preset", {"preset": "balanced", "note": "restore defaults after increment-3 evidence"})
        ev["settings"]["history_top"] = hist[:3]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    (out / f"e2e-increment3-{stamp}.json").write_text(json.dumps(ev, indent=2, default=str))
    passed = sum(1 for v in check.values() if v)
    lines = [f"# Increment 3 end-to-end evidence — {started:%Y-%m-%d %H:%M} UTC", "",
             f"Live run through `{args.api}` in workspace `{wid}` on a seeded SQLite retail database "
             f"({ev['data']['orders']:,} orders). Raw: `e2e-increment3-{stamp}.json`.", "",
             f"**{passed}/{len(check)} checks passed.**", "", "| Check | Result |", "|---|---|"]
    lines += [f"| {k} | {'PASS' if v else '**FAIL**'} |" for k, v in check.items()]
    lines += ["", "## Crawler", "", "Full crawl stats: `" + json.dumps(ev["crawl_full"]["stats"]) + "`", "",
              "| Table | Role | Domain | Grain | Confidence | Description from |", "|---|---|---|---|---|---|"]
    lines += [f"| {n} | {s['role']} | {s['domain']} | {s['grain']} | {s['confidence']} | {s['description_origin']} |"
              for n, s in ev["crawl_full"]["semantics"].items()]
    lines += ["", "PII found by rules: " + ", ".join(f"`{k}` ({v})" for k, v in ev["crawl_full"]["pii"].items()), "",
              "Incremental crawl (nothing changed): `" + json.dumps(ev["crawl_incremental"]["stats"]) + "`", "",
              "Drift crawl changes: `" + json.dumps({k: v for k, v in ev["crawl_drift"]["changes"].items() if v}) + "`", "",
              "## SQL explanation (no model)", "", f"- accepted: {ev['explain']['accepted']}",
              f"- rejected by the gateway: {ev['explain']['rejected']}", "",
              "## Analysis under the token_saver preset", "",
              f"Run `{ev['analysis']['run_id']}`: cost ${ev['analysis']['cost_usd']}, {ev['analysis']['tokens']} tokens, "
              f"{ev['analysis']['model_calls']} model calls, {len(ev['analysis']['deterministic_skips'])} deterministic skips.", ""]
    lines += [f"- verified: {t}" for t in ev["analysis"]["verified"]]
    lines += ["", "Planted effects: " + ", ".join(f"{k}={'found' if v else 'missed'}" for k, v in ev["analysis"]["planted"].items()),
              "", "Deterministic skips: " + "; ".join(f"{p} ({r})" for p, r in ev["analysis"]["deterministic_skips"][:12]), "",
              "## Token-savings ledger (last day)", "", "```json", json.dumps(ev["token_savings"].get("totals", ev["token_savings"]),
                                                                              indent=1, default=str)[:3000], "```"]
    (out / f"e2e-increment3-{stamp}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[: len(check) + 7]))
    print(f"wrote {out / f'e2e-increment3-{stamp}.md'}")
    return 0 if passed == len(check) else 1


if __name__ == "__main__":
    sys.exit(main())
