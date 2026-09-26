""""Why this number?" (P7-08, spec v4 §7): resolve a displayed number through the records that produced it.

Each number of a finding's displayed wording is re-bound to the finding's typed facts (P4-03) and then
followed through six links, each with its **current** state:

1. ``fact``             the typed fact the number binds to (value, unit, subject, baseline);
2. ``step``             the step that computed it (hypothesis + `AnalysisSpec`, method and its version);
3. ``query_receipt``    the governed queries (SQL, query hash, result hash) and whether the stored receipt
                        still carries the result hash the fact quotes;
4. ``data_version``     the snapshot the run read (manifest entry) against the snapshot now;
5. ``semantic_version`` the approved KPI definitions the verdict depended on, recorded vs now;
6. ``verdict``          the verification record (ADR-0020): ACTIVE, VOID with its cause, SUPERSEDED, legacy.

Link states: ``ok`` | ``changed`` | ``void`` | ``failed`` | ``broken`` | ``unknown`` | ``not_applicable``.
A broken or voided link is returned like any other, never dropped; the number's overall ``state`` is its
worst link. Read-only: nothing here re-runs a query or changes a record.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.evidence import Fact, ManifestEntry
from analystos.core.errors import NotFound

SEVERITY = {"ok": 0, "not_applicable": 0, "unknown": 1, "changed": 2, "failed": 3, "void": 4, "broken": 5}
LINKS = ("fact", "step", "query_receipt", "data_version", "semantic_version", "verdict")


def _link(name: str, link_state: str, reason: str | None = None, **detail: Any) -> dict[str, Any]:
    return {"link": name, "state": link_state, "reason": reason, "detail": detail}


def _worst(links: list[dict[str, Any]]) -> str:
    return max((lk["state"] for lk in links), key=lambda s: SEVERITY.get(s, 1), default="unknown")


def _facts(bundle: Mapping[str, Any]) -> list[Fact]:
    out = []
    for f in (bundle.get("claim") or {}).get("facts") or []:
        try:
            out.append(Fact.model_validate(f))
        except Exception:  # noqa: BLE001 - a malformed legacy fact is reported as unbound, not raised
            continue
    return out


def _mentions(session: Session, ins: Any, facts: list[Fact]) -> list[dict[str, Any]]:
    """The numbers of the wording shown now, bound again to the facts (the stored binding if that fails)."""
    from analystos.db.models import Experiment, Hypothesis
    from analystos.evidence.facts import bind_finding

    h = session.get(Hypothesis, ins.hypothesis_id) if ins.hypothesis_id else None
    exp = session.scalar(select(Experiment).where(Experiment.hypothesis_id == ins.hypothesis_id, Experiment.role == "primary")
                         .order_by(Experiment.created_at.desc()).limit(1)) if ins.hypothesis_id else None
    if h is not None and exp is not None and facts:
        try:
            return [m.model_dump() for m in bind_finding(dict(h.spec or {}), dict(exp.result or {}), facts, ins.title,
                                                         ins.finding).mentions]
        except Exception:  # noqa: BLE001 - fall back to what REV bound
            pass
    return list((((ins.evidence_bundle or {}).get("claim") or {}).get("binding") or {}).get("mentions") or [])


def _step_link(session: Session, ins: Any, record: Any | None) -> dict[str, Any]:
    from analystos import methods
    from analystos.db.models import Experiment, Hypothesis
    from analystos.evidence.verification import UNKNOWABLE, current_version
    from analystos.registries.hypotheses import spec_hash

    h = session.get(Hypothesis, ins.hypothesis_id) if ins.hypothesis_id else None
    if h is None:
        return _link("step", "broken", "the hypothesis that produced this finding is missing", hypothesis_id=ins.hypothesis_id)
    exp = session.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.role == "primary")
                         .order_by(Experiment.created_at.desc()).limit(1))
    spec = dict(h.spec or {})
    manifest = methods.current().manifests.get(str(spec.get("method")))
    detail = {"hypothesis_id": h.id, "code": h.code, "statement": h.statement, "status": h.status,
              "method": spec.get("method"), "method_version": manifest.version if manifest is not None else None,
              "spec_hash": spec_hash(spec), "experiment_id": exp.id if exp is not None else None}
    if exp is None:
        return _link("step", "broken", "no primary experiment recorded for this step", **detail)
    recorded = {(d["kind"], d["ref"]): d["version_hash"] for d in (record.dependencies if record is not None else [])}
    changed = []
    for (kind, ref), version in sorted(recorded.items()):
        if kind in ("query", "method") and current_version(session, kind, ref) not in (version, UNKNOWABLE):
            changed.append(f"{kind} {ref}")
    if changed:
        return _link("step", "changed", "changed since the verdict: " + ", ".join(sorted(changed)), **detail)
    return _link("step", "ok", **detail)


def _receipt_link(session: Session, ins: Any, fact: Fact | None) -> dict[str, Any]:
    from analystos.db.models import QueryExecution
    from analystos.knowledge.attested import sql_hash

    receipts = {r.get("query_id"): r for r in ((ins.evidence_bundle or {}).get("data") or {}).get("queries") or []}
    ids = list(fact.query_ids) if fact is not None and fact.query_ids else list(receipts)
    if not ids:
        ids = [e["id"] for e in ins.evidence or [] if e.get("type") == "query"]
    if not ids:
        return _link("query_receipt", "broken", "no governed query receipt is recorded for this number", queries=[])
    rows = {q.id: q for q in session.scalars(select(QueryExecution).where(QueryExecution.id.in_(ids),
                                                                          QueryExecution.workspace_id == ins.workspace_id))}
    quoted = set(fact.result_hashes) if fact is not None else set()
    queries, problems = [], []
    for qid in ids:
        q, r = rows.get(qid), receipts.get(qid) or {}
        recorded = r.get("result_hash")
        if q is None:
            problems.append(f"query {qid} is missing")
            queries.append({"query_id": qid, "kind": "sql", "state": "broken", "recorded_result_hash": recorded})
            continue
        matches = (recorded is None or q.result_hash == recorded) and (not quoted or q.result_hash in quoted)
        if not matches:
            problems.append(f"query {qid} result hash differs from the receipt the finding quotes")
        if q.status != "ok":
            problems.append(f"query {qid} status is {q.status}")
        queries.append({"query_id": q.id, "kind": "sql", "sql": q.executed_sql or q.sql, "query_hash": sql_hash(q.executed_sql or q.sql),
                        "result_hash": q.result_hash, "recorded_result_hash": recorded, "rows": q.row_count,
                        "source_id": q.source_id, "created_at": q.created_at.isoformat() if q.created_at else None,
                        "state": "ok" if matches and q.status == "ok" else "broken"})
    return _link("query_receipt", "broken" if problems else "ok", "; ".join(problems) or None, queries=queries)


def _data_link(session: Session, ins: Any) -> dict[str, Any]:
    from analystos.evidence.manifest import changed, current_entry

    rec = ((ins.evidence_bundle or {}).get("data") or {}).get("entry")
    if not rec:
        return _link("data_version", "broken", "no data-version manifest entry was recorded (legacy finding)")
    entry = ManifestEntry.model_validate(rec)
    now = current_entry(session, entry.asset, entry.source_id)
    detail = {"asset": entry.asset, "source_id": entry.source_id, "mode": entry.mode, "recorded_version": entry.version,
              "current_version": now.version, "recorded_rows": entry.rows, "current_rows": now.rows,
              "version_basis": entry.version_basis, "staged_at": entry.staged_at, "observed_at": entry.observed_at}
    if entry.mode != "staged" or not entry.version:
        return _link("data_version", "unknown", "pushdown or unversioned source: replay is best effort", **detail)
    why = changed(entry, now)
    if why:
        return _link("data_version", "changed", why, **detail)
    if now.mode != "staged" or not now.version:
        return _link("data_version", "unknown", "the snapshot has no fixed version now", **detail)
    return _link("data_version", "ok", **detail)


def _semantic_link(session: Session, record: Any | None) -> dict[str, Any]:
    from analystos.db.models import SemanticMetric
    from analystos.evidence.verification import current_version

    deps = [d for d in (record.dependencies if record is not None else []) if d["kind"] == "semantic"]
    if not deps:
        return _link("semantic_version", "not_applicable",
                     "the number was computed from the step's AnalysisSpec, not from a governed metric definition", metrics=[])
    metrics, changed = [], []
    for d in deps:
        workspace_id, _, name = d["ref"].partition("/")
        row = session.scalar(select(SemanticMetric).where(SemanticMetric.workspace_id == workspace_id, SemanticMetric.name == name,
                                                          SemanticMetric.status == "approved")
                             .order_by(SemanticMetric.version.desc()).limit(1))
        same = current_version(session, "semantic", d["ref"]) == d["version_hash"]
        if not same:
            changed.append(name)
        metrics.append({"name": name, "approved_version": row.version if row is not None else None,
                        "approved_hash": row.content_hash if row is not None else None, "state": "ok" if same else "changed"})
    return _link("semantic_version", "changed" if changed else "ok",
                 ("approved definition changed since the verdict: " + ", ".join(changed)) if changed else None, metrics=metrics)


def _verdict_link(state: Mapping[str, Any]) -> dict[str, Any]:
    detail = {k: state.get(k) for k in ("record_id", "state", "badge", "verdict", "verifier", "fingerprint", "created_at",
                                         "void", "flags")}
    s = state.get("state")
    if s is None:
        return _link("verdict", "unknown", "no verification record for this finding", **detail)
    if s == "VOID":
        v = state.get("void") or {}
        return _link("verdict", "void", f"{v.get('kind')}: {v.get('reason')}", **detail)
    if s == "SUPERSEDED":
        return _link("verdict", "changed", "a newer verdict (or a replan) superseded this one", **detail)
    if s == "LEGACY":
        return _link("verdict", "unknown", "verified before dependency fingerprints; shown without a verification badge", **detail)
    if s == "PENDING":
        return _link("verdict", "unknown", "verification in progress", **detail)
    if state.get("verdict") != "verified":
        return _link("verdict", "failed", "REV did not verify this finding", **detail)
    return _link("verdict", "ok", **detail)


def explain_insight(session: Session, ins: Any, *, number: str | None = None, fact_id: str | None = None) -> dict[str, Any]:
    """Every displayed number of a finding (or the one asked for) with its six links."""
    from analystos.db.models import VerificationRecord
    from analystos.evidence.verification import insight_states

    bundle = dict(ins.evidence_bundle or {})
    facts = _facts(bundle)
    by_id = {f.id: f for f in facts}
    state = insight_states(session, [ins.id])[ins.id]
    record = session.get(VerificationRecord, state["record_id"]) if state.get("record_id") else None
    shared = [_step_link(session, ins, record), None, _data_link(session, ins), _semantic_link(session, record),
              _verdict_link(state)]
    mentions = _mentions(session, ins, facts)
    if fact_id is not None:
        mentions = [m for m in mentions if m.get("fact_id") == fact_id] or \
            ([{"text": fact_id, "value": by_id[fact_id].value, "unit": by_id[fact_id].unit, "fact_id": fact_id}]
             if fact_id in by_id else [])
    if number is not None:
        wanted = number.strip()
        mentions = [m for m in mentions if m.get("text", "").strip() == wanted or wanted in m.get("text", "")]
    if (number is not None or fact_id is not None) and not mentions:
        raise NotFound(f"{number or fact_id!r} is not a number displayed by finding {ins.code}")
    numbers = []
    for m in mentions:
        fact = by_id.get(m.get("fact_id") or "")
        if fact is not None:
            fact_link = _link("fact", "ok", fact_id=fact.id, **{k: v for k, v in fact.model_dump().items() if k != "id"})
        else:
            fact_link = _link("fact", "broken", m.get("problem") or "the number binds to no computed fact", fact_id=None)
        links = [fact_link, shared[0], _receipt_link(session, ins, fact), *shared[2:]]
        numbers.append({"text": m.get("text"), "value": m.get("value"), "unit": m.get("unit"), "state": _worst(links),
                        "links": links})
    return {"subject": {"type": "insight", "id": ins.id, "code": ins.code, "run_id": ins.run_id, "title": ins.title,
                        "finding": ins.finding, "status": ins.status},
            "verification_state": state, "state": _worst([lk for n in numbers for lk in n["links"]]) if numbers else
            _worst([lk for lk in shared if lk is not None]),
            "numbers": numbers}


def explain_run(session: Session, run_id: str) -> dict[str, Any]:
    """Every number of every finding a run's report presents (its verified findings), resolved."""
    from analystos.db.models import AnalysisRun, Insight, by_code

    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise NotFound("run not found")
    findings = [explain_insight(session, ins) for ins in session.scalars(
        select(Insight).where(Insight.run_id == run_id, Insight.status == "verified").order_by(*by_code(Insight.code)))]
    numbers = [n for f in findings for n in f["numbers"]]
    counts: dict[str, int] = {}
    for n in numbers:
        counts[n["state"]] = counts.get(n["state"], 0) + 1
    return {"run_id": run_id, "findings": findings, "numbers": len(numbers), "by_state": counts}


__all__ = ["LINKS", "SEVERITY", "explain_insight", "explain_run"]
