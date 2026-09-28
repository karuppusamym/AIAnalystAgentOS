"""What a workspace's data is good for: a deterministic "shape" of the selected tables from what the catalog already
measured (roles, keys, profiles, relationships), each pattern with its evidence and the next step it opens.

Patterns per table: `event_log` (process mining), `time_series` (trend over time), `ml_candidate` (features and target
candidates for an experiment), and the table's own role (`fact`, `dimension`, `bridge`, `reference`). Workspace level:
`star` (facts joined to dimensions: analytics style), `normalized` (many joined tables: operational style), `flat`
(one table), `event_log`. Everything is a suggestion: a person confirms or dismisses a pattern once and the answer is
kept on the table (`semantics.shape`, kept across crawls), so it is not asked again. No query and no model runs here;
`propose` (a model reading names only) is for the tables the rules leave unexplained and is verified against the profile
before anything is shown."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import utcnow
from analystos.db.models import SourceAsset, SourceColumn
from analystos.semantic import suggest as sug
from analystos.skills import process_mining as pm
from analystos.skills.profiling import column_is_sensitive

VERSION = "data-shape/1"
KINDS = ("event_log", "time_series", "ml_candidate", "fact", "dimension", "bridge", "reference")
ML_MIN_ROWS = 200
ML_MIN_FEATURES = 3
TIME_SERIES_MIN_ROWS = 24
MAX_TARGETS = 5
FEATURE_ROLES = {"measure", "amount", "percent", "duration", "dimension", "code", "flag"}
STEPS = {  # pattern -> (action, label); the page maps an action to its screen
    "event_log": ("process_analysis", "Analyse the process"),
    "time_series": ("investigation", "Investigate the trend"),
    "ml_candidate": ("experiment", "Try a prediction"),
    "fact": ("ask", "Ask questions about it"),
}


def _pattern(kind: str, confidence: float, reasons: list[str], **detail: Any) -> dict[str, Any]:
    action = STEPS.get(kind)
    return {"kind": kind, "confidence": round(max(0.0, min(0.95, confidence)), 2), "reasons": reasons, "detail": detail,
            "next_step": {"action": action[0], "label": action[1]} if action else None, "origin": "rules", "state": "suggested"}


def _targets(cols: list[SourceColumn], rows: int | None) -> list[dict[str, Any]]:
    """Columns a model could predict: a two-sided flag or a small set of classes (classification), an amount or a
    duration (regression). Sensitive columns are never offered."""
    out: list[dict[str, Any]] = []
    for c in cols:
        sem, p = c.semantics or {}, c.profile or {}
        role = sem.get("semantic_role")
        if column_is_sensitive(c.tags, sem) or c.is_key or role in ("foreign_key", "identifier"):
            continue
        null_rate = p.get("null_rate")
        if isinstance(null_rate, int | float) and null_rate > 0.5:
            continue
        distinct = p.get("distinct")
        if role == "flag" or c.data_type in ("boolean", "bool"):
            true_count, non_null = p.get("true_count"), p.get("non_null")
            if true_count is not None and non_null:
                share = true_count / non_null
                if not 0.02 <= share <= 0.98:
                    continue  # nearly constant: nothing to learn
                out.append({"column": c.name, "kind": "classification", "why": f"true in {share:.0%} of rows"})
            else:
                out.append({"column": c.name, "kind": "classification", "why": "a yes/no flag"})
        elif role in ("dimension", "code") and isinstance(distinct, int | float) and 2 <= distinct <= 10:
            out.append({"column": c.name, "kind": "classification", "why": f"{int(distinct)} classes"})
        elif role in ("measure", "amount", "duration", "percent"):
            out.append({"column": c.name, "kind": "regression", "why": f"a {role} column"})
    rank = {"classification": 0, "regression": 1}
    return sorted(out, key=lambda t: (rank[t["kind"]], t["column"]))[:MAX_TARGETS]


def _features(cols: list[SourceColumn]) -> list[str]:
    return [c.name for c in cols if (c.semantics or {}).get("semantic_role") in FEATURE_ROLES and not c.is_key
            and not column_is_sensitive(c.tags, c.semantics)]


def table_patterns(asset: SourceAsset, cols: list[SourceColumn], view: dict[str, Any], allowed: set[str] | None,
                   linked: int) -> list[dict[str, Any]]:
    """The patterns one table shows, strongest first. `allowed`: the columns the caller may read (None = all)."""
    cols = [c for c in cols if allowed is None or c.name in allowed]
    rows = asset.row_count
    role = view["role"]
    out: list[dict[str, Any]] = []
    log = pm.detect_event_log({"name": asset.name, "role": (asset.semantics or {}).get("role"), "row_count": rows,
                               "columns": [{"name": c.name, "data_type": c.data_type, "is_key": c.is_key, "tags": c.tags or [],
                                            "profile": c.profile or {}, "semantic_role": (c.semantics or {}).get("semantic_role")}
                                           for c in cols]})
    if log:
        out.append(_pattern("event_log", 0.4 + 0.05 * float(log["score"]), list(log["reasons"])[:4], mapping=log["mapping"]))
    measures = [m for m in view["measures"] if allowed is None or m in allowed]
    if view["time_column"] and measures and (rows is None or rows >= TIME_SERIES_MIN_ROWS) and role in sug.FACT_ROLES | {"unknown"}:
        out.append(_pattern("time_series", 0.8 if role in sug.FACT_ROLES else 0.5,
                            [f"a time column ({view['time_column']}) and a measure ({', '.join(measures[:2])})"],
                            time_column=view["time_column"], measures=measures[:3]))
    features, targets = _features(cols), _targets(cols, rows)
    if rows and rows >= ML_MIN_ROWS and len(features) >= ML_MIN_FEATURES and targets:
        out.append(_pattern("ml_candidate", 0.7 if role in sug.FACT_ROLES else 0.55,
                            [f"{rows:,} rows, {len(features)} usable columns, {len(targets)} column(s) worth predicting"],
                            targets=targets, features=features[:12]))
    if role in ("fact", "event"):
        out.append(_pattern("fact", 0.75, [f"role {role}: {view.get('grain') or 'one row per record'}"], linked=linked))
    elif role in ("dimension", "reference", "bridge"):
        out.append(_pattern(role, 0.75, [f"role {role}"], linked=linked))
    return sorted(out, key=lambda p: -p["confidence"])


def workspace_shape(tables: list[dict[str, Any]], rels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the whole set looks like, from table roles and the (validated or pending) joins between them."""
    by_id = {t["asset_id"]: t for t in tables}
    facts = {t["asset_id"] for t in tables if t["role"] in sug.FACT_ROLES}
    dims = {t["asset_id"] for t in tables if t["role"] in sug.DIMENSION_ROLES}
    joined = [(r["from"]["asset_id"], r["to"]["asset_id"]) for r in rels if r["from"]["asset_id"] in by_id and r["to"]["asset_id"] in by_id]
    star = [(a, b) for a, b in joined if (a in facts and b in dims) or (b in facts and a in dims)]
    out: list[dict[str, Any]] = []
    if star:
        fact_tables = sorted({a if a in facts else b for a, b in star})
        dim_tables = sorted({b if a in facts else a for a, b in star})
        out.append({"kind": "star", "label": "Star schema (analytics style)", "reasons": [
            f"{len(fact_tables)} fact table(s) joined to {len(dim_tables)} dimension(s)"],
            "tables": [by_id[i]["name"] for i in fact_tables + dim_tables]})
    elif len(tables) >= 3 and len({a for pair in joined for a in pair}) >= 3:
        out.append({"kind": "normalized", "label": "Normalized operational model (OLTP style)", "reasons": [
            f"{len(tables)} tables joined by {len(joined)} reference(s), without a fact/dimension split"], "tables": []})
    elif len(tables) == 1:
        out.append({"kind": "flat", "label": "One flat table", "reasons": ["a single selected table"], "tables": [tables[0]["name"]]})
    elif tables:
        out.append({"kind": "unlinked", "label": "Tables without joins between them", "reasons": [
            "no measured or declared join links the selected tables: review relationships in Data"], "tables": []})
    return out


def _load(session: Session, workspace_id: str, scope: Any = None, assets: list[SourceAsset] | None = None
          ) -> tuple[list[SourceAsset], dict[str, list[SourceColumn]]]:
    if assets is None:
        return sug._load(session, workspace_id)
    cols: dict[str, list[SourceColumn]] = {a.id: [] for a in assets}
    if assets:
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(list(cols))).order_by(SourceColumn.asset_id,
                                                                                                             SourceColumn.ordinal)):
            cols[c.asset_id].append(c)
    return assets, cols


def analyze(session: Session, workspace_id: str, *, assets: list[SourceAsset] | None = None,
            allowed: Callable[[str], set[str]] | None = None) -> dict[str, Any]:
    """The shape of the workspace's selected tables. `assets` / `allowed(fq)`: the caller's scope (default: every selected
    table, every column). A pattern a person confirmed is `confirmed`; one they dismissed is left out."""
    assets, cols = _load(session, workspace_id, assets=assets)
    views = {a.id: sug._table(a, cols[a.id], None) for a in assets}
    rels = sug.relationships(session, workspace_id, assets) if assets else []
    linked: dict[str, int] = {}
    for r in rels:
        for side in ("from", "to"):
            linked[r[side]["asset_id"]] = linked.get(r[side]["asset_id"], 0) + 1
    tables = []
    for a in assets:
        fq = f"{a.schema_name}.{a.name}"
        marks = (a.semantics or {}).get("shape") or {}
        confirmed, dismissed = set(marks.get("confirmed") or []), set(marks.get("dismissed") or [])
        patterns = [p for p in table_patterns(a, cols[a.id], views[a.id], allowed(fq) if allowed else None, linked.get(a.id, 0))
                    if p["kind"] not in dismissed]
        for p in patterns:
            if p["kind"] in confirmed:
                p["state"] = "confirmed"
        proposed = [p for p in marks.get("proposed") or [] if isinstance(p, dict) and p.get("kind") not in dismissed
                    and p.get("kind") not in {q["kind"] for q in patterns}]
        for p in proposed:
            p["state"] = "confirmed" if p["kind"] in confirmed else "proposed"
        patterns += proposed
        tables.append({"asset_id": a.id, "fq": fq, "name": a.name, "business_name": a.business_name, "role": views[a.id]["role"],
                       "row_count": a.row_count, "patterns": patterns, "unexplained": not patterns})
    summary = {k: sum(1 for t in tables for p in t["patterns"] if p["kind"] == k) for k in KINDS}
    return {"version": VERSION, "workspace": workspace_shape(list(views.values()), rels), "tables": tables,
            "summary": {k: v for k, v in summary.items() if v}, "generated_at": utcnow().isoformat()}


def mark(session: Session, workspace_id: str, asset_id: str, kind: str, decision: str, user_id: str) -> dict[str, Any]:
    """A person's answer about one pattern of one table: confirm it or dismiss it. Kept on the table."""
    if kind not in KINDS:
        raise InvalidInput(f"unknown pattern {kind!r}")
    if decision not in ("confirm", "dismiss", "reset"):
        raise InvalidInput("decision must be confirm, dismiss or reset")
    a = session.get(SourceAsset, asset_id)
    if a is None or a.workspace_id != workspace_id:
        raise NotFound("table not found in this workspace")
    marks = dict((a.semantics or {}).get("shape") or {})
    confirmed, dismissed = set(marks.get("confirmed") or []), set(marks.get("dismissed") or [])
    confirmed.discard(kind)
    dismissed.discard(kind)
    if decision == "confirm":
        confirmed.add(kind)
    elif decision == "dismiss":
        dismissed.add(kind)
    marks.update(confirmed=sorted(confirmed), dismissed=sorted(dismissed), by=user_id, at=utcnow().isoformat())
    a.semantics = {**(a.semantics or {}), "shape": marks}
    return {"asset_id": a.id, "kind": kind, "decision": decision, "confirmed": marks["confirmed"], "dismissed": marks["dismissed"]}


# ------------------------------------------------------------------------------------ model proposal
PURPOSE = "data_shape_proposal"
PROMPT = "data_shape_proposal.v1"
MAX_PROPOSE_TABLES = 6


def _verify_event_log(proposal: dict[str, Any], cols: dict[str, SourceColumn], rows: int | None) -> tuple[dict[str, Any] | None, str]:
    """Code decides: the named columns exist, the time column is a time type, the activity has a plausible number of
    distinct values and the case column repeats (fewer distinct values than rows) - all from the crawl's profile."""
    mapping = {k: proposal.get(k) for k in ("case_column", "activity_column", "timestamp_column")}
    resource = proposal.get("resource_column")
    for key, name in mapping.items():
        if name not in cols:
            return None, f"{key} {name!r} is not a column"
    if len({*mapping.values()}) != 3:
        return None, "case, activity and time must be different columns"
    if not pm._is_time(str(cols[mapping["timestamp_column"]].data_type)):
        return None, "the time column is not a date or time"
    for name in (mapping["case_column"], mapping["activity_column"]):
        if column_is_sensitive(cols[name].tags, cols[name].semantics):
            return None, f"{name} is sensitive"
    act = (cols[mapping["activity_column"]].profile or {}).get("distinct")
    case = (cols[mapping["case_column"]].profile or {}).get("distinct")
    if not isinstance(act, int | float) or not 2 <= act <= 200:
        return None, "the activity column does not have 2 to 200 distinct values (or is not profiled)"
    if rows and isinstance(case, int | float) and case >= rows * 0.9:
        return None, "the case column is almost unique: no case has more than one event"
    if resource is not None and resource not in cols:
        resource = None
    return {**mapping, "resource_column": resource}, "verified against the profile"


def _near_miss(cols: dict[str, SourceColumn]) -> bool:
    """A time column and a text column with a plausible number of distinct values: worth a model's second look."""
    times = [c for c in cols.values() if pm._is_time(str(c.data_type))]
    texts = [c for c in cols.values() if pm._is_text(str(c.data_type))
             and isinstance((c.profile or {}).get("distinct"), int | float) and 2 <= (c.profile or {})["distinct"] <= 200]
    return bool(times) and len(texts) >= 2


def propose(session: Session, workspace_id: str, *, router: Any) -> dict[str, Any]:
    """Ask the model about tables the rules did not recognise as an event log but that could be one (a time column and text
    columns with few distinct values; bounded, names, types and distinct counts only), verify each answer
    in code and keep the verified ones as `proposed` patterns for a person to confirm. Off or unavailable: nothing runs and the
    avoided call is recorded as a saving."""
    from analystos.agents.prompts import prompt, prompt_version_id
    from analystos.core.errors import AnalystOSError
    from analystos.llm.cache import estimate_tokens
    from analystos.runtime.context import workspace_call_ctx

    shape = analyze(session, workspace_id)
    candidates = [t for t in shape["tables"] if t["role"] not in ("dimension", "reference", "bridge")
                  and not any(p["kind"] == "event_log" for p in t["patterns"])]
    ids = [t["asset_id"] for t in candidates]
    by_id = {a.id: a for a in session.scalars(select(SourceAsset).where(SourceAsset.id.in_(ids)))} if ids else {}
    cols_by: dict[str, dict[str, SourceColumn]] = {i: {} for i in by_id}
    if by_id:
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(list(by_id)))):
            if not column_is_sensitive(c.tags, c.semantics):
                cols_by[c.asset_id][c.name] = c
    todo = [t for t in candidates if _near_miss(cols_by[t["asset_id"]])][:MAX_PROPOSE_TABLES]
    report: dict[str, Any] = {"called": False, "considered": len(todo), "proposed": 0, "rejected": []}
    if not todo:
        return {**report, "skipped": "no table is left that could be an event log"}
    payload_tables = [{"table": by_id[t["asset_id"]].name, "rows": t["row_count"], "columns": [
        {"name": c.name, "type": c.data_type, "role": (c.semantics or {}).get("semantic_role"),
         "distinct": (c.profile or {}).get("distinct")} for c in cols_by[t["asset_id"]].values()][:60]} for t in todo]
    system = prompt(PROMPT)
    version = prompt_version_id(PROMPT, system)
    payload = json.dumps({"tables": payload_tables}, separators=(",", ":"), sort_keys=True, default=str)
    ctx = workspace_call_ctx(workspace_id, agent_id="catalog_steward", prompt_version=version)
    mode = router.mode(PURPOSE)
    if mode == "off" or not router.available(PURPOSE, ctx):
        router.record_skip(PURPOSE, ctx, estimated_tokens=estimate_tokens(system + payload) + 300,
                           reason=f"mode={mode}: unexplained tables stay unexplained until a person looks")
        return {**report, "skipped": "model off" if mode == "off" else "no model available"}
    try:
        resp = router.complete_json(PURPOSE, system, payload, ctx=ctx, max_tokens=1200)
    except AnalystOSError as exc:
        return {**report, "skipped": f"model call failed: {exc.message[:120]}"}
    report["called"] = True
    data = getattr(resp, "data", None)
    answers = data.get("tables") if isinstance(data, dict) else None
    names = {by_id[t["asset_id"]].name: t["asset_id"] for t in todo}
    for item in answers if isinstance(answers, list) else []:
        if not isinstance(item, dict) or item.get("table") not in names or item.get("pattern") != "event_log":
            continue  # only the pattern a column mapping can verify is accepted from a model
        asset_id = names[item["table"]]
        mapping, note = _verify_event_log(item, cols_by[asset_id], by_id[asset_id].row_count)
        if mapping is None:
            report["rejected"].append({"table": item["table"], "why": note})
            continue
        why = " ".join(str(item.get("reason") or "").split())[:200]
        a = by_id[asset_id]
        marks = dict((a.semantics or {}).get("shape") or {})
        marks["proposed"] = [p for p in marks.get("proposed") or [] if p.get("kind") != "event_log"] + [
            {**_pattern("event_log", 0.5, [why or "proposed by the model", note], mapping=mapping),
             "origin": "model", "state": "proposed"}]
        a.semantics = {**(a.semantics or {}), "shape": marks}
        report["proposed"] += 1
    return report
