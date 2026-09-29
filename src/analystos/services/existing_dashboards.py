"""Existing-dashboard mode (v1 §35, BI-011/012): import a BI dashboard the platform did not build, map it to the
workspace (sources, semantic metrics), re-execute every chart through the gateway to verify the numbers the BI
tool shows, flag drift and mismatched definitions, and propose improvements.

Invariants:

* Reads only. Inspection and chart data are GETs through the BI adapter (``publishing.base.DashboardSource``);
  every re-executed chart is SQL the platform composes from the chart's definition (``BIChartQuery``) and runs
  through ``QueryGateway.execute`` under the caller's own scope, so row filters and denied columns apply.
* Proposals are never applied on import. A write back to the BI tool is an approval bound to the import's
  fingerprint and the exact changes (``bi.dashboard.update``); execution calls ``verify_for_execution``,
  re-inspects the live dashboard and refuses if it changed since the import, then consumes the approval.
* A proposed semantic metric goes through the semantic layer's own approval workflow; nothing is approved here.
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from analystos.contracts.bi import BIChartQuery
from analystos.contracts.policy import DataScope
from analystos.core.errors import AnalystOSError, Conflict, InvalidInput, PolicyDenied
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import Approval, BiDashboardImport, SemanticMetric, User
from analystos.events.bus import emit
from analystos.governance.approvals import consume, request_approval, verify_for_execution
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, load_in_workspace, require_role, resolve_scope, scoped_loader
from analystos.publishing.base import DashboardSource, get_dashboard_source

ACTION = "bi.dashboard.update"
PURPOSE = "bi.dashboard.verify"
DEFAULT_ROW_LIMIT = 10_000
REL_TOLERANCE = 1e-6
_SEVERITY = {"number_mismatch": "high", "definition_mismatch": "high", "unmapped_dataset": "medium",
             "duplicate_definition": "medium", "unmapped_metric": "medium", "structure_changed": "medium",
             "uncertified_metric": "low", "not_reproducible": "info"}


# ------------------------------------------------------------------------------------ adapter
def dashboard_source(destination: str, source: DashboardSource | None = None) -> DashboardSource:
    if source is not None:
        return source
    if destination == "superset":
        from analystos.services.platform_settings import get as platform

        if not platform().features.superset_publishing:
            raise InvalidInput("Superset is turned off by the administrator")
    return get_dashboard_source(destination)


def fingerprint(inspection: dict[str, Any]) -> str:
    """What the dashboard *is*: its charts' queries, its datasets' SQL and metric definitions, filters and layout.
    URLs and publish state are left out, so the same dashboard fingerprints the same wherever it is served."""
    return stable_hash({"title": inspection.get("title"), "charts": sorted(
        ({"id": str(c.get("id")), "viz": c.get("viz_type"), "query": c.get("query")} for c in inspection.get("charts") or []),
        key=lambda c: c["id"]),
        "datasets": sorted(({"id": str(d.get("id")), "sql": d.get("sql"), "schema": d.get("schema"), "name": d.get("name"),
                             "metrics": sorted(((m.get("name"), m.get("expression"), bool(m.get("certified")))
                                                for m in d.get("metrics") or []), key=str)}
                            for d in inspection.get("datasets") or []), key=lambda d: d["id"]),
        "filters": inspection.get("filters") or [], "layout": inspection.get("layout") or []})


def _chart_signature(chart: dict[str, Any]) -> str:
    return stable_hash({"viz": chart.get("viz_type"), "query": chart.get("query")})


# ------------------------------------------------------------------------------------ mapping
def _match_asset(fq: str, scope: DataScope) -> str | None:
    wanted = fq.lower()
    exact = [a for a in scope.assets if a.lower() == wanted]
    if exact:
        return exact[0]
    if "." not in wanted:
        by_name = [a for a in scope.assets if a.lower().rsplit(".", 1)[-1] == wanted]
        return by_name[0] if len(by_name) == 1 else None
    return None


def _dataset_tables(ds: dict[str, Any]) -> list[str]:
    if not ds.get("sql"):
        name = str(ds.get("name") or "")
        return [f"{ds['schema']}.{name}" if ds.get("schema") else name]
    try:
        tree = sqlglot.parse_one(str(ds["sql"]), read="postgres")
    except sqlglot.errors.SqlglotError:
        return []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out = []
    for t in tree.find_all(exp.Table):
        if not t.name or (not t.db and t.name.lower() in ctes):
            continue
        out.append(f"{t.db}.{t.name}" if t.db else (f"{ds['schema']}.{t.name}" if ds.get("schema") else t.name))
    return list(dict.fromkeys(out))


def workspace_database(workspace_id: str) -> str:
    """The BI database AnalystOS provisions for a workspace (one per workspace, its own BI login)."""
    from analystos.publishing.superset import SupersetPublisher

    return SupersetPublisher.database_name(workspace_id)


def source_databases(session: Session, workspace_id: str) -> dict[str, set[str]]:
    """source id -> the BI databases that are that source. A staged source is the workspace's own analytics database;
    any source can name the BI database that connects to it (`config.superset_database`). A dataset on any other
    database is never mapped, even when a table name matches: its numbers come from somewhere else."""
    from analystos.db.models import Source

    out: dict[str, set[str]] = {}
    for src in session.scalars(select(Source).where(Source.workspace_id == workspace_id)):
        names = {str((src.config or {}).get("superset_database") or "")} - {""}
        if src.execution_mode == "staged":
            names.add(workspace_database(workspace_id))
        out[src.id] = names
    return out


def owned_dataset(ds: dict[str, Any], workspace_id: str) -> bool:
    """A dataset this workspace may change: on its own BI database, or published by it (`aos_<ws>_`). A dataset
    other teams built on a shared database is never written, whatever this workspace's approvers decide."""
    from analystos.publishing.superset import SupersetPublisher

    return ds.get("database") == workspace_database(workspace_id) or \
        str(ds.get("name") or "").lower().startswith(SupersetPublisher.chart_prefix(workspace_id).lower())


def map_datasets(inspection: dict[str, Any], scope: DataScope, databases: dict[str, set[str]]) -> dict[str, dict[str, Any]]:
    """Each BI dataset -> the workspace assets it reads. Mapped only when every table it reads is in the caller's
    scope, all of them come from one source (the gateway runs one source per statement), and the dataset's BI
    database is that source (`databases`, from `source_databases`)."""
    out: dict[str, dict[str, Any]] = {}
    for ds in inspection.get("datasets") or []:
        tables = _dataset_tables(ds)
        assets = {t: _match_asset(t, scope) for t in tables}
        mapped = [a for a in assets.values() if a]
        sources = sorted({scope.asset_sources.get(a, "") for a in mapped})
        unmapped = [t for t, a in assets.items() if not a]
        reason = None
        if not tables:
            reason = "the dataset's SQL could not be read"
        elif unmapped:
            reason = f"not in this workspace's selected sources: {', '.join(unmapped)}"
        elif len(sources) != 1:
            reason = "the dataset reads tables of more than one source"
        elif ds.get("database") not in databases.get(sources[0], set()):
            reason = (f"its BI database {ds.get('database')!r} is not bound to source {sources[0]} (a staged source is the "
                      "workspace's analytics database; set superset_database in the source config for any other)")
        out[str(ds.get("id"))] = {"dataset_id": str(ds.get("id")), "name": ds.get("name"), "kind": ds.get("kind"),
                                  "database": ds.get("database"), "owned": owned_dataset(ds, scope.workspace_id),
                                  "assets": mapped if reason is None else [], "unmapped": unmapped,
                                  "source_id": sources[0] if len(sources) == 1 else None,
                                  "mapped": reason is None, "reason": reason}
    return out


def map_metrics(inspection: dict[str, Any], approved: dict[str, SemanticMetric]) -> list[dict[str, Any]]:
    """Every metric a chart shows -> the approved semantic metric it is (by name, else by definition)."""
    from analystos.semantic.ossie import normalize_expression

    by_expr: dict[str, list[SemanticMetric]] = {}
    for row in approved.values():
        by_expr.setdefault(row.normalized_expression, []).append(row)
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for chart in inspection.get("charts") or []:
        q = chart.get("query") or {}
        for m in q.get("metrics") or []:
            key = (str(q.get("dataset_id")), m["label"])
            if key in seen:
                seen[key]["charts"].append(chart.get("id"))
                continue
            entry: dict[str, Any] = {"dataset_id": key[0], "label": m["label"], "expression": m.get("expression"),
                                     "saved": bool(m.get("saved")), "certified": bool(m.get("certified")),
                                     "charts": [chart.get("id")], "semantic_metric": None, "semantic_metric_id": None,
                                     "approved_expression": None}
            norm = normalize_expression(m["expression"]) if m.get("expression") else None
            named = approved.get(m["label"])
            same = by_expr.get(norm or "", [])
            if named is not None:
                entry.update(semantic_metric=named.name, semantic_metric_id=named.id, approved_expression=named.expression,
                             status="matches" if named.normalized_expression == norm else "definition_mismatch")
            elif same:
                entry.update(semantic_metric=same[0].name, semantic_metric_id=same[0].id,
                             approved_expression=same[0].expression, status="same_definition_other_name")
            else:
                entry["status"] = "unmapped"
            seen[key] = entry
    return list(seen.values())


# ------------------------------------------------------------------------------------ SQL
def _relation(ds: dict[str, Any], mapped: dict[str, Any], dialect: str) -> exp.Expression:
    from analystos.skills import sqlbuild as sb

    if not ds.get("sql"):
        return sb.table(mapped["assets"][0])
    inner = sqlglot.parse_one(str(ds["sql"]), read=dialect)
    return exp.Subquery(this=inner, alias=exp.TableAlias(this=sb.ident("aos_bi_dataset")))


def _condition(f: dict[str, Any], dialect: str) -> exp.Expression:
    from analystos.skills import sqlbuild as sb

    c, op, v = sb.col(f["column"]), f["op"], f.get("value")
    if op == "IS NULL":
        return sb.is_null(c)
    if op == "IS NOT NULL":
        return sb.not_null(c)
    if op in ("IN", "NOT IN"):
        cond = exp.In(this=c, expressions=[sb.lit(x, dialect) for x in (v or [])])
        return exp.Not(this=cond) if op == "NOT IN" else cond
    cls = {"==": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE, "LIKE": exp.Like}[op]
    return cls(this=c, expression=sb.lit(v, dialect))


def chart_sql(q: BIChartQuery, ds: dict[str, Any], mapped: dict[str, Any], dialect: str) -> str:
    """The chart's aggregate as one SELECT the gateway validates: group by the time bucket and dimensions,
    each metric under its BI label, the chart's own filters, its row limit."""
    from analystos.skills import sqlbuild as sb

    if q.time_column and dialect not in sb.DIALECTS:
        raise InvalidInput(f"time buckets are not reproducible on {dialect}")
    keys: list[tuple[str, exp.Expression]] = []
    if q.time_column and q.time_grain:
        keys.append((q.time_column, sb.trunc_expr(sb.ts_expr(q.time_column, dialect), q.time_grain, dialect)))
    keys += [(d, sb.col(d)) for d in q.dimensions if d != q.time_column]
    select_list = [e.copy().as_(sb.ident(name)) for name, e in keys]
    select_list += [sqlglot.parse_one(str(m.expression), read=dialect).as_(sb.ident(m.label)) for m in q.metrics]
    tree = exp.select(*select_list).from_(_relation(ds, mapped, dialect))
    conds = [_condition(f.model_dump(), dialect) for f in q.filters]
    conds += [sb.paren(sqlglot.parse_one(s, read=dialect)) for s in q.sql_filters]
    where = sb.and_all(conds)
    if where is not None:
        tree = tree.where(where)
    if keys:
        tree = tree.group_by(*[e.copy() for _, e in keys])
    tree = tree.limit(q.row_limit or DEFAULT_ROW_LIMIT)
    return tree.sql(dialect=dialect)


# ------------------------------------------------------------------------------------ comparison
def _norm_key(v: Any, temporal: bool) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, int | float):
        if temporal and abs(v) >= 1e11:  # epoch milliseconds (how BI tools return a time axis)
            return _dt.datetime.fromtimestamp(v / 1000, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
        return repr(round(float(v), 9))
    s = str(v)
    if temporal and len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return (s[:10] + "T" + (s[11:19] if len(s) >= 19 else "00:00:00")).replace(" ", "T")
    return s


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _same(a: Any, b: Any) -> bool:
    x, y = _num(a), _num(b)
    if x is None and y is None:
        return a in (None, "") and b in (None, "") or str(a) == str(b)
    if x is None or y is None:
        return False
    return abs(x - y) <= max(1e-9, REL_TOLERANCE * max(abs(x), abs(y)))


def _index(columns: list[str], rows: list[list[Any]], keys: list[str], labels: list[str], time_col: str | None
           ) -> dict[tuple, dict[str, Any]] | None:
    """rows -> {key tuple: {metric label: value}}; unpivots a BI result whose series became columns
    ("metric, series" or, with one metric, "series"). None when the shape cannot be matched."""
    pos = {c: i for i, c in enumerate(columns)}
    if all(k in pos for k in keys) and all(m in pos for m in labels):
        return {tuple(_norm_key(r[pos[k]], k == time_col) for k in keys): {m: r[pos[m]] for m in labels} for r in rows}
    base = [k for k in keys if k in pos]
    series = [k for k in keys if k not in pos]
    if not series or len(base) + len(series) != len(keys):
        return None
    out: dict[tuple, dict[str, Any]] = {}
    for c in columns:
        if c in base:
            continue
        parts = [p.strip() for p in str(c).split(",")]
        if parts[0] in labels and len(parts) == len(series) + 1:
            metric, values = parts[0], parts[1:]
        elif len(labels) == 1 and len(parts) == len(series):
            metric, values = labels[0], parts
        else:
            return None
        for r in rows:
            key_map = {k: r[pos[k]] for k in base} | dict(zip(series, values, strict=True))
            key = tuple(_norm_key(key_map[k], k == time_col) for k in keys)
            if r[pos[c]] is not None:
                out.setdefault(key, {})[metric] = r[pos[c]]
    return out


def compare(q: BIChartQuery, governed: dict[str, Any], bi: dict[str, Any], *, truncated: bool) -> dict[str, Any]:
    keys = ([q.time_column] if q.time_column else []) + [d for d in q.dimensions if d != q.time_column]
    labels = [m.label for m in q.metrics]
    ours = _index(governed["columns"], governed["rows"], keys, labels, q.time_column) or {}
    theirs = _index(list(bi.get("columns") or []), list(bi.get("rows") or []), keys, labels, q.time_column)
    if theirs is None:
        return {"status": "governed_only", "note": "the BI tool's result has a shape the platform cannot line up "
                f"with the chart's query ({len(bi.get('columns') or [])} columns)"}
    mismatches, compared = [], 0
    for key in sorted(set(ours) & set(theirs), key=str):
        for m in labels:
            if m not in theirs[key]:
                continue
            compared += 1
            a, b = ours[key].get(m), theirs[key].get(m)
            if not _same(a, b):
                x, y = _num(a), _num(b)
                mismatches.append({"key": list(key), "metric": m, "governed": a, "bi": b,
                                   "delta": (x - y) if x is not None and y is not None else None})
    only_bi = sorted(set(theirs) - set(ours), key=str)
    only_gov = sorted(set(ours) - set(theirs), key=str)
    limited = truncated or (q.row_limit is not None and len(theirs) >= q.row_limit)
    missing = bool(only_bi or only_gov) and not limited
    status = "mismatch" if mismatches or missing else ("verified" if compared else "governed_only")
    return {"status": status, "compared": compared, "mismatches": mismatches[:20],
            "only_in_bi": len(only_bi), "only_in_governed": [list(k) for k in only_gov[:10]],
            "partial": limited}


# ------------------------------------------------------------------------------------ verification
def verify_charts(inspection: dict[str, Any], datasets: dict[str, dict[str, Any]], scope: DataScope, *, gateway: Any,
                  source: DashboardSource, actor: str) -> list[dict[str, Any]]:
    """Re-execute every chart through the gateway and compare with what the BI tool shows for it."""
    by_id = {str(d.get("id")): d for d in inspection.get("datasets") or []}
    out = []
    for chart in inspection.get("charts") or []:
        q = BIChartQuery.model_validate(chart.get("query") or {"dataset_id": str(chart.get("dataset_id") or "")})
        entry: dict[str, Any] = {"chart_id": chart.get("id"), "name": chart.get("name"), "viz_type": chart.get("viz_type"),
                                 "dataset_id": q.dataset_id, "status": None, "reason": None, "sql": None, "query_id": None}
        mapped = datasets.get(q.dataset_id)
        if q.unsupported:
            entry.update(status="not_reproducible", reason=q.unsupported)
        elif mapped is None or not mapped["mapped"]:
            entry.update(status="unmapped", reason=(mapped or {}).get("reason") or "the chart's dataset is not visible")
        if entry["status"]:
            out.append(entry)
            continue
        dialect = scope.source_dialects.get(mapped["source_id"] or "", "postgres")
        try:
            entry["sql"] = chart_sql(q, by_id[q.dataset_id], mapped, dialect)
            result = gateway.execute(scope, entry["sql"], actor=actor, purpose=PURPOSE, max_rows=q.row_limit or DEFAULT_ROW_LIMIT)
        except (AnalystOSError, sqlglot.errors.SqlglotError) as exc:
            entry.update(status="refused", reason=getattr(exc, "message", None) or str(exc))
            out.append(entry)
            continue
        governed = {"columns": result.columns, "rows": result.rows}
        # Receipts, not rows: an import is read by other members whose scope may be narrower than the importer's.
        entry.update(query_id=result.query_id, result_hash=result.result_hash,
                     governed={"row_count": result.row_count, "truncated": result.truncated})
        # A chart over a denied column never gets here (the gateway refused it); row filters still narrow what this
        # caller may see of rows the BI tool reads in full, so then its numbers are not read at all.
        if any(scope.row_filters.get(a) for a in mapped["assets"]):
            entry.update(status="filtered", reason="row filters narrow your access to this data compared with the BI "
                         "tool's, so the dashboard's numbers are not read for comparison")
            out.append(entry)
            continue
        bi = source.chart_data(chart.get("id"))
        entry["bi_read"] = True
        if bi.get("error"):
            entry.update(status="governed_only", reason=f"the BI tool's numbers could not be read: {bi['error']}")
        else:
            entry["bi"] = {"row_count": len(bi.get("rows") or [])}
            entry["comparison"] = compare(q, governed, bi, truncated=result.truncated)
            entry["status"] = entry["comparison"]["status"]
            entry["reason"] = entry["comparison"].get("note")
        out.append(entry)
    return out


# ------------------------------------------------------------------------------------ findings, proposals
def _finding(kind: str, message: str, **ref: Any) -> dict[str, Any]:
    return {"id": stable_hash({"kind": kind, **ref})[:16], "kind": kind, "severity": _SEVERITY[kind], "message": message, **ref}


def findings_for(datasets: dict[str, dict[str, Any]], metrics: list[dict[str, Any]], verification: list[dict[str, Any]],
                 inspection: dict[str, Any], previous: BiDashboardImport | None) -> list[dict[str, Any]]:
    out = []
    for d in datasets.values():
        if not d["mapped"]:
            out.append(_finding("unmapped_dataset", f"Dataset {d['name']} is not mapped: {d['reason']}.", dataset_id=d["dataset_id"]))
    for m in metrics:
        where = {"dataset_id": m["dataset_id"], "metric": m["label"]}
        if m["status"] == "definition_mismatch":
            out.append(_finding("definition_mismatch", f"{m['label']} computes {m['expression']} in the BI tool; the approved "
                                f"definition is {m['approved_expression']}.", **where))
        elif m["status"] == "same_definition_other_name":
            out.append(_finding("duplicate_definition", f"{m['label']} is the approved metric {m['semantic_metric']} under "
                                "another name.", **where))
        elif m["status"] == "unmapped":
            out.append(_finding("unmapped_metric", f"{m['label']} has no approved definition in the semantic layer.", **where))
        elif m["status"] == "matches" and m["saved"] and not m["certified"]:
            out.append(_finding("uncertified_metric", f"{m['label']} matches its approved definition but is not marked "
                                "certified in the BI tool.", **where))
    for v in verification:
        if v["status"] == "mismatch":
            n = len((v.get("comparison") or {}).get("mismatches") or [])
            out.append(_finding("number_mismatch", f"Chart {v['name']}: the governed re-execution differs from the dashboard "
                                f"({n} value(s) differ" + (", rows missing on one side" if (v.get("comparison") or {}).get(
                                    "only_in_bi") or (v.get("comparison") or {}).get("only_in_governed") else "") + ").",
                                chart_id=v["chart_id"]))
        elif v["status"] in ("not_reproducible", "refused"):
            out.append(_finding("not_reproducible", f"Chart {v['name']} was not verified: {v['reason']}.", chart_id=v["chart_id"]))
    if previous is not None and previous.fingerprint != fingerprint(inspection):
        before = {str(c.get("id")): _chart_signature(c) for c in (previous.inspection or {}).get("charts") or []}
        after = {str(c.get("id")): _chart_signature(c) for c in inspection.get("charts") or []}
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        out.append(_finding("structure_changed", f"The dashboard changed in the BI tool since import v{previous.version}"
                            + (f" (charts {', '.join(changed)})" if changed else " (datasets, filters or layout)") + ".",
                            since_version=previous.version, charts=changed))
    return out


def proposals_for(metrics: list[dict[str, Any]], findings: list[dict[str, Any]],
                  datasets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic proposals. `write_back` ones change the BI tool (approval-bound) and exist only for datasets this
    workspace owns; on a shared dataset the same change is advice for its owner. `platform` ones propose a semantic
    metric (its own approval); the rest are advice."""
    out = []
    by_metric = {(f.get("dataset_id"), f.get("metric")): f for f in findings if f.get("metric")}
    owned = {k for k, d in datasets.items() if d.get("owned")}

    def add(kind: str, title: str, finding: dict[str, Any] | None, *, write_back: bool = False, platform: bool = False,
            change: dict[str, Any] | None = None) -> None:
        pid = stable_hash({"kind": kind, "change": change, "finding": finding and finding["id"]})[:16]
        out.append({"id": pid, "kind": kind, "title": title, "finding_id": finding and finding["id"],
                    "write_back": write_back, "platform": platform, "change": change, "status": "proposed"})

    for m in metrics:
        f = by_metric.get((m["dataset_id"], m["label"]))
        if m["status"] in ("definition_mismatch", "matches") and m["saved"] and m["dataset_id"] not in owned:
            if m["status"] == "definition_mismatch" or not m["certified"]:
                add("ask_dataset_owner", f"{m['label']} lives on a dataset this workspace does not own: ask its owner to "
                    f"{'align it with' if m['status'] == 'definition_mismatch' else 'certify it as'} the approved metric "
                    f"{m['semantic_metric']}", f)
        elif m["status"] == "definition_mismatch" and m["saved"]:
            add("align_metric", f"Align {m['label']} with the approved definition ({m['approved_expression']}) and certify it",
                f, write_back=True, change={"type": "set_dataset_metric", "dataset_id": m["dataset_id"], "metric": m["label"],
                                            "expression": m["approved_expression"], "certify": True,
                                            "details": f"Approved semantic metric {m['semantic_metric']}"})
        elif m["status"] == "definition_mismatch":
            add("align_chart_metric", f"Replace the inline {m['label']} on charts {m['charts']} with the approved metric "
                f"{m['semantic_metric']} (edit the chart in the BI tool)", f)
        elif m["status"] == "matches" and m["saved"] and not m["certified"]:
            add("certify_metric", f"Mark {m['label']} certified: it matches the approved definition", f, write_back=True,
                change={"type": "set_dataset_metric", "dataset_id": m["dataset_id"], "metric": m["label"],
                        "expression": m["expression"], "certify": True,
                        "details": f"Approved semantic metric {m['semantic_metric']}"})
        elif m["status"] == "same_definition_other_name":
            add("use_approved_name", f"{m['label']} duplicates the approved metric {m['semantic_metric']}: use that metric in "
                "the dashboard instead of a copy", f)
        elif m["status"] == "unmapped" and m.get("expression"):
            add("propose_semantic_metric", f"Propose {m['label']} ({m['expression']}) to the semantic layer for approval", f,
                platform=True, change={"metric": m["label"], "expression": m["expression"], "dataset_id": m["dataset_id"]})
    for f in findings:
        if f["kind"] == "number_mismatch":
            add("review_numbers", "Review the chart's numbers: the dashboard and the governed re-execution disagree", f)
        elif f["kind"] == "unmapped_dataset":
            add("select_source", "Select the dataset's tables in a workspace source so its charts can be verified", f)
    return out


# ------------------------------------------------------------------------------------ service
def import_out(row: BiDashboardImport) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for v in row.verification or []:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    return {"id": row.id, "workspace_id": row.workspace_id, "destination": row.destination, "external_id": row.external_id,
            "version": row.version, "title": row.title, "status": row.status, "fingerprint": row.fingerprint,
            "previous_fingerprint": row.previous_fingerprint, "drift": bool(row.previous_fingerprint
                                                                             and row.previous_fingerprint != row.fingerprint),
            "url": (row.inspection or {}).get("url"), "inspection": row.inspection, "mapping": row.mapping,
            "verification": row.verification, "verification_counts": counts, "findings": row.findings,
            "proposals": row.proposals, "applied": row.applied, "imported_by": row.imported_by,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def _latest(session: Session, workspace_id: str, destination: str, external_id: str) -> BiDashboardImport | None:
    return session.scalar(select(BiDashboardImport).where(
        BiDashboardImport.workspace_id == workspace_id, BiDashboardImport.destination == destination,
        BiDashboardImport.external_id == external_id).order_by(BiDashboardImport.version.desc()).limit(1))


def list_candidates(session: Session, user: User, workspace_id: str, destination: str = "superset", *,
                    source: DashboardSource | None = None) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "editor")  # as the Superset metadata crawl: the BI tool's whole catalog
    src = dashboard_source(destination, source)
    imported = {r.external_id: r for r in session.scalars(select(BiDashboardImport).where(
        BiDashboardImport.workspace_id == workspace_id, BiDashboardImport.destination == destination)
        .order_by(BiDashboardImport.version))}
    return [{**d, "last_import": ({"id": imported[str(d["id"])].id, "version": imported[str(d["id"])].version,
                                   "status": imported[str(d["id"])].status} if str(d["id"]) in imported else None)}
            for d in src.list_dashboards(workspace_id)]


def list_imports(session: Session, user: User, workspace_id: str) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    rows = session.scalars(select(BiDashboardImport).where(BiDashboardImport.workspace_id == workspace_id)
                           .order_by(BiDashboardImport.created_at.desc(), BiDashboardImport.version.desc()))
    return [{k: v for k, v in import_out(r).items() if k not in ("inspection", "mapping", "verification")} for r in rows]


@scoped_loader
def get_import(session: Session, user: User, workspace_id: str, import_id: str) -> dict[str, Any]:
    """Analyst and above: mismatch values were computed under the importer's scope (no raw rows are kept)."""
    return import_out(load_in_workspace(session, BiDashboardImport, import_id, workspace_id, user=user, minimum="analyst",
                                        label="dashboard import"))


def import_dashboard(session: Session, user: User, workspace_id: str, dashboard_id: str, *, destination: str = "superset",
                     source: DashboardSource | None = None, gateway: Any = None) -> dict[str, Any]:
    """BI-011: inspect, map, verify, flag and propose. Writes one new import version; changes nothing in the BI tool."""
    from analystos.artifacts.registry import link, link_queries
    from analystos.semantic.service import approved_metrics

    scope = resolve_scope(session, user, workspace_id, minimum_role="editor")
    src = dashboard_source(destination, source)
    if gateway is None:
        from analystos.runtime.context import default_gateway

        gateway = default_gateway()
    inspection = src.inspect_dashboard(dashboard_id, workspace_id=workspace_id)
    external_id = str(inspection.get("id") or dashboard_id)
    datasets = map_datasets(inspection, scope, source_databases(session, workspace_id))
    metrics = map_metrics(inspection, approved_metrics(session, workspace_id))
    verification = verify_charts(inspection, datasets, scope, gateway=gateway, source=src, actor=f"user:{user.id}")
    previous = _latest(session, workspace_id, destination, external_id)
    findings = findings_for(datasets, metrics, verification, inspection, previous)
    proposals = proposals_for(metrics, findings, datasets)
    statuses = {v["status"] for v in verification}
    serious = any(f["severity"] in ("high", "medium") for f in findings)
    status = "attention" if serious else ("verified" if statuses and statuses <= {"verified"} else "unverified")
    row = BiDashboardImport(id=new_id("bdi"), workspace_id=workspace_id, destination=destination, external_id=external_id,
                            version=(previous.version if previous else 0) + 1, title=str(inspection.get("title") or external_id)[:300],
                            status=status, fingerprint=fingerprint(inspection),
                            previous_fingerprint=previous.fingerprint if previous else None, inspection=inspection,
                            mapping={"datasets": list(datasets.values()), "metrics": metrics}, verification=verification,
                            findings=findings, proposals=proposals, applied=[], imported_by=user.id)
    session.add(row)
    session.flush()
    node = ("bi_dashboard", row.id)
    for d in datasets.values():
        for asset in d["assets"]:
            link(session, workspace_id, node, "reads", ("table", asset))
    for m in metrics:
        if m["semantic_metric_id"]:
            link(session, workspace_id, node, "uses_metric", ("semantic_metric", m["semantic_metric_id"]))
    link_queries(session, workspace_id, node, [v["query_id"] for v in verification if v.get("query_id")])
    payload = {"import_id": row.id, "dashboard_id": external_id, "destination": destination, "version": row.version,
               "status": status, "findings": len(findings)}
    emit(workspace_id, "bi_dashboard.imported", payload, actor=f"user:{user.id}", session=session)
    if previous is not None and previous.fingerprint != row.fingerprint:
        emit(workspace_id, "bi_dashboard.drift_detected", {**payload, "since_version": previous.version},
             actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "bi_dashboard.imported", workspace_id=workspace_id, target=row.id,
          details={"destination": destination, "dashboard_id": external_id, "fingerprint": row.fingerprint,
                   "verification": {s: sum(1 for v in verification if v["status"] == s) for s in statuses},
                   # the BI tool ran these charts' own saved queries (with its own login) for the comparison
                   "bi_chart_data_read": [v["chart_id"] for v in verification if v.get("bi_read")]}, session=session)
    return import_out(row)


def _chosen(row: BiDashboardImport, proposal_ids: list[str]) -> list[dict[str, Any]]:
    by_id = {p["id"]: p for p in row.proposals or []}
    unknown = [p for p in proposal_ids if p not in by_id]
    if unknown or not proposal_ids:
        raise InvalidInput(f"unknown proposal(s) for import {row.id}: {', '.join(unknown) or 'none given'}")
    return [by_id[p] for p in sorted(set(proposal_ids))]


def update_payload(row: BiDashboardImport, proposal_ids: list[str]) -> dict[str, Any]:
    """What the approval binds: this import (and the dashboard fingerprint it saw) and the exact changes."""
    chosen = _chosen(row, proposal_ids)
    advisory = [p["id"] for p in chosen if not p["write_back"]]
    if advisory:
        raise InvalidInput(f"proposal(s) {', '.join(advisory)} do not change the BI tool; they are advice or a semantic "
                           "proposal", details={"proposals": advisory})
    return {"action": ACTION, "workspace_id": row.workspace_id, "import_id": row.id, "destination": row.destination,
            "dashboard_id": row.external_id, "fingerprint": row.fingerprint, "proposals": [p["id"] for p in chosen],
            "changes": [p["change"] for p in chosen]}


@scoped_loader
def request_update(session: Session, user: User, workspace_id: str, import_id: str, proposal_ids: list[str]) -> dict[str, Any]:
    """BI-012: ask for approval to write the chosen proposals back to the BI tool. Nothing changes yet."""
    row = load_in_workspace(session, BiDashboardImport, import_id, workspace_id, user=user, minimum="editor",
                            label="dashboard import")
    payload = update_payload(row, proposal_ids)
    approval = request_approval(session, workspace_id=workspace_id, run_id=None, action=ACTION, payload=payload,
                                plan_hash=None, policy_version=get_workspace(session, workspace_id).policy_version,
                                requested_by=user.id, risk_tier="high", destination=f"{row.destination}:dashboard:{row.external_id}",
                                affected_assets=sorted({f"{row.destination}:dataset:{c['dataset_id']}" for c in payload["changes"]}),
                                evidence={"import_id": row.id, "import_version": row.version, "title": row.title,
                                          "changes": payload["changes"],
                                          "findings": [f for f in row.findings or [] if f["id"] in
                                                       {p["finding_id"] for p in row.proposals if p["id"] in payload["proposals"]}]})
    if not any(a.get("approval_id") == approval.id for a in row.applied or []):
        row.applied = [*(row.applied or []), {"approval_id": approval.id, "status": "requested", "proposals": payload["proposals"],
                                              "by": user.id, "at": utcnow().isoformat()}]
    emit(workspace_id, "bi_dashboard.update_requested", {"import_id": row.id, "approval_id": approval.id,
                                                         "proposals": payload["proposals"]}, actor=f"user:{user.id}", session=session)
    return {"status": "approval_required", "approval_id": approval.id, "expires_at": approval.expires_at.isoformat()}


@scoped_loader
def execute_update(session: Session, user: User, workspace_id: str, import_id: str, approval_id: str, *,
                   source: DashboardSource | None = None) -> dict[str, Any]:
    """Apply an approved update: verify the approval against the payload rebuilt from the import, refuse if the
    live dashboard changed since the import, consume the approval, then write."""
    row = load_in_workspace(session, BiDashboardImport, import_id, workspace_id, user=user, minimum="editor",
                            label="dashboard import")
    approval = session.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
    if approval is None or approval.workspace_id != workspace_id or approval.action != ACTION \
            or (approval.payload or {}).get("import_id") != row.id:
        raise PolicyDenied("the approval does not authorize an update of this dashboard import")
    payload = update_payload(row, list((approval.payload or {}).get("proposals") or []))
    verify_for_execution(session, approval_id, payload=payload, plan_hash=None)
    src = dashboard_source(row.destination, source)
    live = src.inspect_dashboard(row.external_id, workspace_id=workspace_id)
    if fingerprint(live) != row.fingerprint:
        raise Conflict(f"the dashboard changed in {row.destination} since import v{row.version}; import it again and "
                       "request the update on the new import")
    foreign = sorted({str(c["dataset_id"]) for c in payload["changes"]} -
                     {str(d.get("id")) for d in live.get("datasets") or [] if owned_dataset(d, workspace_id)})
    if foreign:
        raise PolicyDenied(f"dataset(s) {', '.join(foreign)} are not owned by this workspace; they are never written")
    consume(session, approval)
    record: dict[str, Any] = {"approval_id": approval_id, "proposals": payload["proposals"], "by": user.id,
                              "at": utcnow().isoformat()}
    try:
        record.update(status="applied", changed=src.apply_changes(live, payload["changes"]))
    except Exception as exc:  # noqa: BLE001 - the approval stays consumed: record the failure, never retry silently
        record.update(status="failed", error=getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}",
                      note="earlier changes of this update may have been applied; import the dashboard again to see")
    row.applied = [a for a in row.applied or [] if a.get("approval_id") != approval_id] + [record]
    emit(workspace_id, "bi_dashboard.updated", {"import_id": row.id, "approval_id": approval_id, "status": record["status"]},
         actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "bi_dashboard.updated", workspace_id=workspace_id, target=row.id,
          decision="allow" if record["status"] == "applied" else "error",
          details={"approval_id": approval_id, "changes": payload["changes"], "result": record.get("changed") or record.get("error")},
          session=session)
    return {**record, "next": "import the dashboard again to verify the change"}


@scoped_loader
def propose_metric(session: Session, user: User, workspace_id: str, import_id: str, proposal_id: str) -> dict[str, Any]:
    """A `propose_semantic_metric` proposal -> a semantic metric proposal awaiting its own approval."""
    from analystos.contracts.semantic import DialectExpression, SemanticMetricDef
    from analystos.semantic import service as semantic

    row = load_in_workspace(session, BiDashboardImport, import_id, workspace_id, user=user, minimum="editor",
                            label="dashboard import")
    p = _chosen(row, [proposal_id])[0]
    if p["kind"] != "propose_semantic_metric":
        raise InvalidInput(f"proposal {proposal_id} is not a semantic metric proposal")
    name = p["change"]["metric"]
    try:
        defn = SemanticMetricDef(name=name, expressions=[DialectExpression(expression=p["change"]["expression"])],
                                 description=f"Imported from {row.destination} dashboard {row.title!r}")
    except ValueError as exc:
        raise InvalidInput(f"{name!r} cannot be a semantic metric name: rename it in the proposal first") from exc
    # A person asked for it: user validation applies (and a rejected definition is proposed again, not silently reused);
    # the BI origin is the lineage source.
    metric, created = semantic.propose_metric(session, workspace_id, defn, proposed_by=user.id, via="user",
                                              source=("bi_dashboard", row.id))
    return {"status": "proposed" if created else "exists", "metric": metric.name, "version": metric.version,
            "approval_id": metric.approval_id}

