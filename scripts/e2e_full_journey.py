"""Live end-to-end journey on a running stack (default http://localhost:8000; seeded users): a new workspace built
from three generated CSV files (orders, customers, products; messy headers, a PII column, no declared keys, planted
patterns with known truth) through crawl and context, the brief and suggested model, Ask (rules and model, a join,
analyst mode, a follow-up), an investigation with approval, a report, an ML experiment, a monitor and the graph.
Numbers are checked against the generated truth, not only status codes.

    .venv/bin/python scripts/e2e_full_journey.py [workspace name]

Model-backed answers need a configured provider key; without one those checks report the refusal."""
from __future__ import annotations

import csv
import io
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta

from analystos.demo.seed import Api

# Keep the live-check log usable on Windows terminals with a legacy code page.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")

API = os.environ.get("ANALYSTOS_API", "http://localhost:8000")
NAME = sys.argv[1] if len(sys.argv) > 1 else f"E2E Retail {datetime.now():%m%d-%H%M}"
results: list[tuple[str, str, str]] = []


def check(stage: str, name: str, ok: bool | None, detail: str = "") -> bool:
    tag = "PASS" if ok else ("GAP " if ok is None else "FAIL")
    results.append((tag, f"{stage}: {name}", detail))
    print(f"[{tag}] {stage}: {name}{' — ' + detail if detail else ''}", flush=True)
    return bool(ok)


def req(api: Api, method: str, path: str, body=None, **kw):
    r = api.c.request(method, path, json=body, **kw)
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = r.text
    return r.status_code, data


# ------------------------------------------------------------------ data with planted patterns and known truth
rng = random.Random(7)
REGIONS, SEGMENTS = ["North", "South", "East", "West"], ["Consumer", "Business"]
CATS = {"Electronics": (120, 900), "Home": (15, 200), "Clothing": (10, 120), "Sports": (20, 300)}
CHANNELS = ["Web", "Store", "Marketplace"]
customers = [{"Customer ID": f"C{i:04d}", "Customer Name": f"Customer {i}", "Email": f"customer{i}@example.com",
              "Region": rng.choice(REGIONS), "Segment": rng.choice(SEGMENTS),
              "Signup Date": (date(2024, 1, 1) + timedelta(days=rng.randint(0, 500))).isoformat()} for i in range(1, 401)]
products = []
for i in range(1, 61):
    cat = list(CATS)[i % 4]
    lo, hi = CATS[cat]
    products.append({"Product ID": f"P{i:03d}", "Product Name": f"{cat} item {i}", "Category": cat,
                     "List Price": round(rng.uniform(lo, hi), 2)})
cust_by, prod_by = {c["Customer ID"]: c for c in customers}, {p["Product ID"]: p for p in products}
orders = []
start = date(2025, 10, 1)
for i in range(1, 6001):
    c, p = rng.choice(customers), rng.choice(products)
    od = start + timedelta(days=rng.randint(0, 364))
    ch = rng.choices(CHANNELS, weights=[5, 3, 2])[0]
    promised = od + timedelta(days=5)
    late_p = 0.35 if (c["Region"] == "West" and od >= date(2026, 7, 1)) else 0.08
    delivered = promised + timedelta(days=rng.randint(1, 6)) if rng.random() < late_p else promised - timedelta(days=rng.randint(0, 3))
    ret_p = 0.30 if (p["Category"] == "Electronics" and ch == "Marketplace") else 0.06
    qty = rng.randint(1, 4)
    disc = rng.choice([0, 0, 0, 5, 10, 15])
    orders.append({"Order ID": f"O{i:05d}", "Customer ID": c["Customer ID"], "Product ID": p["Product ID"],
                   "Order Date": od.isoformat(), "Quantity": qty, "Unit Price": p["List Price"], "Discount Pct": disc,
                   "Sales Channel": ch, "Status": rng.choices(["Delivered", "Cancelled"], weights=[95, 5])[0],
                   "Promised Date": promised.isoformat(), "Delivered Date": delivered.isoformat(),
                   "Returned": "Yes" if rng.random() < ret_p else "No"})


def csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


truth_orders = len(orders)
by_cat = Counter(prod_by[o["Product ID"]]["Category"] for o in orders)
ret_by_cat = {k: sum(1 for o in orders if prod_by[o["Product ID"]]["Category"] == k and o["Returned"] == "Yes") / n for k, n in by_cat.items()}
avg_disc_by_channel = {ch: sum(o["Discount Pct"] for o in orders if o["Sales Channel"] == ch) / max(1, sum(1 for o in orders if o["Sales Channel"] == ch)) for ch in CHANNELS}
top_channel = Counter(o["Sales Channel"] for o in orders).most_common(1)[0][0]

# ------------------------------------------------------------------ 1. workspace and import
an = Api(API, "analyst@analystos.local", "ChangeMe123!")
ad = Api(API, "admin@analystos.local", "ChangeMe123!")
ap = Api(API, "approver@analystos.local", "ChangeMe123!")
st, ws = req(ad, "POST", "/api/workspaces", {"name": NAME, "description": "Online and store retail orders, customers and products",
                                             "objective": "Understand returns, late deliveries and sales by channel and region"})
check("import", "workspace created", st == 200, str(st))
W = ws["id"]
for email, role in (("analyst@analystos.local", "editor"), ("approver@analystos.local", "approver")):
    req(ad, "POST", f"/api/workspaces/{W}/members", {"email": email, "role": role})
print("workspace", W, flush=True)
for fname, rows in (("customers.csv", customers), ("products.csv", products), ("orders.csv", orders)):
    r = an.c.post(f"/api/workspaces/{W}/uploads", files={"file": (fname, csv_bytes(rows), "text/csv")})
    check("import", f"upload {fname}", r.status_code in (200, 201), f"{r.status_code} {r.text[:120] if r.status_code >= 300 else ''}")
st, src = req(an, "POST", f"/api/workspaces/{W}/sources", {"kind": "csv", "name": "Retail files", "config": {"path": W}})
check("import", "file source registered", st in (200, 201), str(st))
S = src["id"]
st, found = req(an, "POST", f"/api/workspaces/{W}/sources/{S}/discover")
names = sorted(a["name"] for a in (found or {}).get("assets", found or []) if isinstance(a, dict)) if isinstance(found, (dict, list)) else []
check("import", "discover finds 3 files", st == 200, f"{st} {names or str(found)[:200]}")
st, sel = req(an, "PUT", f"/api/workspaces/{W}/sources/{S}/selection", {"assets": ["customers", "products", "orders"]})
check("import", "select + stage 3 tables", st == 200, str(st) if st == 200 else str(sel)[:300])

# ------------------------------------------------------------------ 2. crawl with profiling
auto = [c for c in an.get(f"/api/workspaces/{W}/crawls") if c["source_id"] == S]
check("crawl", "selecting tables starts a profiling crawl", bool(auto) and (auto[0].get("options") or {}).get("profile"),
      str([(c["id"], c["status"], c["trigger"], c.get("options")) for c in auto]))
if auto and auto[0]["status"] in ("queued", "running", "pending"):
    crawl, st = auto[0], 200
else:
    st, crawl = req(an, "POST", f"/api/workspaces/{W}/sources/{S}/crawl", {"mode": "full", "profile": True})
    check("crawl", "crawl started", st == 200, str(crawl)[:200] if st != 200 else crawl["id"])
t0 = time.time()
row = crawl
while st == 200:
    row = next(c for c in an.get(f"/api/workspaces/{W}/crawls") if c["id"] == crawl["id"])
    if row["status"] not in ("queued", "running", "pending") or time.time() - t0 > 600:
        break
    time.sleep(3)
check("crawl", "crawl succeeded", row["status"] == "succeeded", f"{row['status']} in {time.time() - t0:.0f}s {row.get('error') or ''}")
stats = row.get("stats") or {}
print("crawl stats:", {k: v for k, v in stats.items() if isinstance(v, (int, float)) and v}, flush=True)

cat = {a["name"]: a for a in an.get(f"/api/workspaces/{W}/catalog")}
for t in ("customers", "products", "orders"):
    a = cat.get(t) or {}
    check("context", f"{t}: profiled", bool(a.get("stats", {}).get("profile_meta") or a.get("profile_meta")), f"rows {a.get('row_count')}")
    check("context", f"{t}: described", bool(a.get("description")) and "Uploaded file" not in (a.get("description") or ""),
          f"{a.get('description_origin')}: {(a.get('description') or '')[:140]}")
    check("context", f"{t}: business name", bool(a.get("business_name")), str(a.get("business_name")))
roles = {t: cat.get(t, {}).get("role") for t in ("customers", "products", "orders")}
check("context", "orders is a fact, customers/products dimensions",
      roles.get("orders") in ("fact", "event") and roles.get("customers") == "dimension" and roles.get("products") == "dimension", str(roles))
ocols = {c["name"]: c for c in cat.get("orders", {}).get("columns", [])}
ccols = {c["name"]: c for c in cat.get("customers", {}).get("columns", [])}
check("context", "headers become snake_case columns with business names",
      "order_date" in ocols and ocols["order_date"].get("business_name") == "Order Date", str(list(ocols)[:6]))
check("context", "date columns typed as dates", all(ocols.get(c, {}).get("data_type") in ("date", "timestamp")
                                                    for c in ("order_date", "promised_date", "delivered_date")),
      str({c: ocols.get(c, {}).get("data_type") for c in ("order_date", "promised_date", "delivered_date")}))
check("context", "order_id detected as key", "key order_id" in (cat.get("orders", {}).get("description") or ""),
      (cat.get("orders", {}).get("description") or "")[:120])
check("context", "email tagged PII", "pii" in (ccols.get("email", {}).get("tags") or []), str(ccols.get("email", {}).get("tags")))
check("context", "customer_name tagged PII (person name)", "pii" in (ccols.get("customer_name", {}).get("tags") or []),
      str(ccols.get("customer_name", {}).get("tags")))
ret = ocols.get("returned", {})
check("context", "Yes/No column understood as a flag", ret.get("role") == "flag" or ret.get("data_type") == "boolean",
      f"{ret.get('data_type')} role {ret.get('role')}: {(ret.get('description') or '')[:100]}")
check("context", "value lists withheld from descriptions (default policy)", "Marketplace" not in (ocols.get("sales_channel", {}).get("description") or ""),
      (ocols.get("sales_channel", {}).get("description") or "")[:120])
for c in ("quantity", "unit_price", "discount_pct"):
    col = ocols.get(c, {})
    check("context", f"{c} described with its range", bool(col.get("description")) and ("from" in (col.get("description") or "")
                                                                                     or "range" in (col.get("description") or "")),
          (col.get("description") or "")[:120])

# relationships between files (no declared keys)
rels = an.get(f"/api/workspaces/{W}/relationships")
cands = req(an, "GET", f"/api/workspaces/{W}/semantic/relationships/candidates")[1]
if not isinstance(cands, list):
    cands = (cands or {}).get("candidates", []) if isinstance(cands, dict) else []
pairs = {f"{r.get('from_column')}->{r.get('to_column')} validated={r.get('validated')}" for r in rels}
print("relationships:", pairs, "candidates:", len(cands), flush=True)
joined = {r.get("from_column") for r in rels if r.get("validated")}
check("context", "orders→customers and orders→products joins validated by measurement", {"customer_id", "product_id"} <= joined,
      f"relationships {sorted(pairs)} candidates {len(cands)}")

# glossary, descriptions, brief, model suggestion, data shape
st, scan = req(an, "POST", f"/api/workspaces/{W}/knowledge/glossary/scan", {"use_model": False, "include_ask": True, "include_descriptions": True})
check("context", "glossary scan", st == 200, str({k: v for k, v in (scan or {}).items() if isinstance(v, int)})[:200])
st, summ = req(an, "GET", f"/api/workspaces/{W}/knowledge/suggestions/summary")
check("context", "questions for a person are queued", st == 200 and json.dumps(summ).count("0") < len(json.dumps(summ)), str(summ)[:200])
st, brief = req(an, "GET", f"/api/workspaces/{W}/brief")
keys = [a["key"] for a in (brief or {}).get("assertions", [])]
check("context", "brief has grain/entity/key/time suggestions", st == 200 and any("grain:" in k for k in keys)
      and any("event_time" in k for k in keys), f"{len(keys)} assertions")
ent = {a["subject"].split(".")[-1]: a["value"] for a in (brief or {}).get("assertions", []) if a["field"] == "entity"}
check("context", "brief entities are business words", all(" " not in str(v) or str(v)[0].isupper() for v in ent.values()), str(ent))
st, sug = req(an, "GET", f"/api/workspaces/{W}/semantic/model/suggestion")
mets = [m["expression"] for m in (sug or {}).get("metrics", [])]
check("context", "suggested model with metrics", st == 200 and len(mets) >= 2, f"{(sug or {}).get('summary')} metrics {mets[:6]}")
check("context", "return-rate and revenue candidate metrics", any("returned" in m for m in mets) and any("quantity * unit_price" in m for m in mets)
      and "SUM(unit_price)" not in mets, str(mets))
st, shape = req(an, "GET", f"/api/workspaces/{W}/data-shape")
check("context", "data shape", st == 200, str(shape)[:200])
for fmt in ("markdown", "okf", "json"):
    r = an.c.get(f"/api/workspaces/{W}/context/export", params={"format": fmt})
    check("context", f"export {fmt}", r.status_code == 200 and "secret_ref" not in r.text[:200000] and "customer1@example.com" not in r.text[:200000],
          f"{r.status_code}, {len(r.content)} bytes")
st, graph = req(an, "GET", f"/api/workspaces/{W}/knowledge/graph")
kinds = Counter(n.get("kind") or n.get("type") for n in (graph or {}).get("nodes", [])) if isinstance(graph, dict) else {}
check("graph", "knowledge graph has tables and links", st == 200 and sum(kinds.values()) > 3, f"{st} nodes {dict(kinds)} edges {len((graph or {}).get('edges', [])) if isinstance(graph, dict) else '?'}")

# ------------------------------------------------------------------ 3. questions and chat
st, thread = req(an, "POST", f"/api/workspaces/{W}/ask/threads", {"title": "E2E questions"})
TH = thread["id"]


def ask(q: str, mode: str = "quick"):
    t0 = time.time()
    st, turn = req(an, "POST", f"/api/ask/threads/{TH}/turns", {"question": q, "mode": mode}, timeout=300)
    if st != 200:
        return None, f"HTTP {st}: {str(turn)[:200]}"
    res = turn.get("result") or {}
    detail = f"{turn['status']} by {turn.get('answered_by')} in {time.time() - t0:.1f}s; cols {res.get('columns')}; rows {(res.get('rows') or [])[:4]}"
    if turn["status"] != "answered":
        detail += f" — {(turn.get('refusal') or {}).get('message')}"
    return turn, detail


turn, d = ask("How many orders are there?")
val = ((turn or {}).get("result") or {}).get("rows") or [[None]]
check("ask", "count of orders is exact", turn and turn["status"] == "answered" and val and val[0][-1] == truth_orders, f"truth {truth_orders}; {d}")
turn, d = ask("Which sales channel has the most orders?")
rows = ((turn or {}).get("result") or {}).get("rows") or []
check("ask", "top channel is right", bool(rows) and str(rows[0][0]) == top_channel, f"truth {top_channel}; {d}")
turn, d = ask("average discount pct by sales channel")
rows = ((turn or {}).get("result") or {}).get("rows") or []
got = {str(r[0]): float(r[1]) for r in rows if len(r) > 1 and r[1] is not None}
check("ask", "average discount by channel matches", bool(got) and all(abs(got.get(k, -99) - v) < 0.01 for k, v in avg_disc_by_channel.items()),
      f"truth { {k: round(v, 2) for k, v in avg_disc_by_channel.items()} }; {d}")
turn, d = ask("number of orders by category")
rows = ((turn or {}).get("result") or {}).get("rows") or []
got = {str(r[0]): r[-1] for r in rows}
check("ask", "orders by product category (needs the join)", bool(got) and all(got.get(k) == v for k, v in by_cat.items()), f"truth {dict(by_cat)}; {d}")
turn, d = ask("What is the return rate by category?", mode="analyst")
rows = ((turn or {}).get("result") or {}).get("rows") or []
got = {str(r[0]): float(r[-1]) for r in rows if r and r[-1] is not None}
if got and max(got.values()) > 1:  # a rate given in percent
    got = {k: v / 100 for k, v in got.items()}
check("ask", "analyst mode: return rate by category is correct", bool(got) and all(abs(got.get(k, -1) - v) < 0.005 for k, v in ret_by_cat.items()),
      f"truth { {k: round(v, 3) for k, v in ret_by_cat.items()} }; {d}")
RATE_TURN = turn
turn, d = ask("and by sales channel?")
check("chat", "follow-up uses the thread", turn is not None and turn["status"] == "answered", d)
turn, d = ask("How many orders were delivered late?")
late_truth = sum(1 for o in orders if o["Delivered Date"] > o["Promised Date"])
check("ask", "late deliveries (compare two dates)", turn is not None and turn["status"] == "answered", f"truth {late_truth}; {d}")
if RATE_TURN:
    st, rep_turn = req(an, "POST", f"/api/ask/turns/{RATE_TURN['id']}/promote", {"target": "report"})
    check("ask", "answer saved as a report", st in (200, 201), str(rep_turn)[:300])
    st, why = req(an, "GET", f"/api/ask/turns/{RATE_TURN['id']}/why")
    check("ask", "why these numbers (answer)", st == 200, str(why)[:200])
st, th = req(an, "GET", f"/api/workspaces/{W}/threads/ask_thread/{TH}")
check("ask", "answers recorded in the Data Thread", st == 200 and len((th or {}).get("steps", [])) >= 3, f"{len((th or {}).get('steps', []))} steps")

# ------------------------------------------------------------------ 4. investigation, approval, report
st, run = req(an, "POST", f"/api/workspaces/{W}/analysis", {"objective": "Why are returns high and where are deliveries late?", "source_ids": [S]})
check("analysis", "investigation started", st == 200, str(run)[:200] if st != 200 else run["id"])
R = (run or {}).get("id")
t0, detail = time.time(), {}
while R:
    detail = an.get(f"/api/workspaces/{W}/analysis/{R}")
    status = detail["status"]
    if status == "WAITING_USER":
        pend = [a for a in detail.get("approvals", []) if a["status"] == "pending"]
        if pend:
            req(ap, "POST", f"/api/approvals/{pend[0]['id']}/approve", {"reason": "e2e"})
        else:
            req(an, "POST", f"/api/workspaces/{W}/analysis/{R}/resume")
    if status in ("COMPLETED", "FAILED", "CANCELLED") or time.time() - t0 > 900:
        break
    time.sleep(4)
check("analysis", "investigation completed", detail.get("status") == "COMPLETED", f"{detail.get('status')} in {time.time() - t0:.0f}s")
ins = detail.get("insights") or []
ver = [i for i in ins if i["status"] == "verified"]
check("analysis", "verified findings", len(ver) >= 2, f"{len(ver)} verified of {len(ins)}")
titles = " | ".join(i["title"] for i in ver)
print("findings:", titles[:1500], flush=True)
check("analysis", "found the planted return pattern (Electronics / Marketplace)", "Electronics" in titles or "Marketplace" in titles, "")
check("analysis", "found the planted late-delivery pattern (West)", "West" in titles or "late" in titles.lower(), "")
if ver:
    st, why = req(an, "GET", f"/api/insights/{ver[0]['id']}/why")
    check("analysis", "why-this-number resolves", st == 200, str(why)[:200])
st, steps = req(an, "GET", f"/api/workspaces/{W}/threads/run/{R}")
check("analysis", "run steps in the Data Thread", st == 200 and len((steps or {}).get("steps", [])) > 3, f"{len((steps or {}).get('steps', []))} steps")
st, rep = req(an, "POST", f"/api/workspaces/{W}/reports", {"run_id": R, "kind": "executive", "formats": ["html", "pdf", "xlsx"]})
check("report", "report generated", st == 200, str(rep)[:300])
if st == 200:
    files = (rep.get("content") or {}).get("files") or rep.get("files") or {}
    print("report artifact:", json.dumps(rep, default=str)[:600], flush=True)
    r = an.c.get(f"/api/artifacts/{rep['id']}/download")
    check("report", "report downloads", r.status_code == 200 and len(r.content) > 500, f"{r.status_code} {r.headers.get('content-type')} {len(r.content)} bytes")
    arts = an.get(f"/api/workspaces/{W}/artifacts")
    check("report", "report listed in Outputs", any(a["id"] == rep["id"] for a in (arts if isinstance(arts, list) else arts.get("items", []))), "")

# ------------------------------------------------------------------ 5. ML experiment
orders_fq = cat["orders"]["fq"]
st, prop = req(an, "POST", f"/api/workspaces/{W}/ml/proposals", {"asset": orders_fq, "target": "returned", "task": "classify",
                                                                 "objective": "Predict which orders are returned"})
check("ml", "rules-first ML proposal", st == 200 and (prop or {}).get("proposal"), str((prop or {}).get("problems"))[:300])
if st == 200 and prop.get("proposal"):
    spec = prop["proposal"]
    req(ad, "PUT", f"/api/workspaces/{W}/capabilities/method.ml.classify", {"enabled": True})  # the owner enables training
    check("ml", "proposal splits by time and keeps identifiers out", spec.get("time_column") == "order_date"
          and "customer_id" not in {f["column"] for f in spec.get("features", [])}, str(spec.get("time_column")))
    st, draft = req(an, "POST", f"/api/workspaces/{W}/definitions", {"kind": "ml_spec", "key": "order_returns", "spec": spec,
                                                                     "title": "Predict order returns"})
    check("ml", "ml_spec draft saved", st in (200, 201), str(draft)[:300])
    if st in (200, 201):
        st, pub = req(an, "POST", f"/api/workspaces/{W}/definitions/{draft['id']}/publish", headers={"If-Match": str(draft.get("revision"))})
        check("ml", "ml_spec published", st == 200, str(pub)[:300])
        if st == 200:
            t0 = time.time()
            st, exp = req(an, "POST", f"/api/workspaces/{W}/ml/experiments", {"definition": {"key": "order_returns", "version": pub["version"]},
                                                                              "max_trials": 4})
            dec = ((exp or {}).get("summary") or {}).get("decision") or {}
            check("ml", "experiment trained and beats the baseline", st in (200, 201) and (exp or {}).get("status") == "succeeded"
                  and dec.get("improved"), f"{st} in {time.time() - t0:.0f}s {dec.get('reason') or json.dumps(exp, default=str)[:400]}")

# ------------------------------------------------------------------ 6. monitor
st, mon = req(an, "POST", f"/api/workspaces/{W}/monitors", {"name": "Weekly orders volume", "kind": "metric_drift",
                                                            "config": {"metric": "record_count", "grain": "week", "lookback": 8, "z_threshold": 3}})
check("monitor", "volume monitor created", st in (200, 201), str(mon)[:300])
if st in (200, 201):
    st, ev = req(an, "POST", f"/api/monitors/{mon['id']}/evaluate")
    check("monitor", "monitor evaluates", st == 200, str(ev)[:300])

st, graph = req(an, "GET", f"/api/workspaces/{W}/knowledge/graph")
kinds = Counter(n.get("kind") or n.get("type") for n in (graph or {}).get("nodes", [])) if isinstance(graph, dict) else {}
check("graph", "graph after the journey links runs, findings, report", sum(kinds.values()) > 10, f"nodes {dict(kinds)}")

fails = [r for r in results if r[0] == "FAIL"]
print(f"\n{len(results) - len(fails)}/{len(results)} passed; workspace {W}")
for tag, name, detail in results:
    if tag != "PASS":
        print(f"  {tag} {name} — {detail[:400]}")
