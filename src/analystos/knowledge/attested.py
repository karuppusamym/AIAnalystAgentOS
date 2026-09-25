"""OKF `Attested Computation` documents: a verified finding as knowledge (P4-K08; P4-K04 extends it).

The minimal shape this module writes (docs/10-architecture/okf-profile.md § Attested Computation):

    type: Attested Computation
    title: <finding title>
    status: draft | stable                  # draft until a human approves it in the review queue
    stale_after: <ISO instant>              # §5.5: the evidence is re-checked or dropped after this
    verified: [{by: process:analystos-rev, at}, {by: human:<user>, at}]   # §5.2, human once approved
    analystos:
      kind: attested_computation
      computation:
        query_hash:   sha256 of the SQL as executed (per query, first = primary)
        result_hash:  the gateway's hash of the result rows
        q_value:      Benjamini-Hochberg adjusted p-value of the primary test
        effect_size:  {value, label}
        verified_by:  who attested the numbers (REV = the deterministic checks)
        run_id, insight_id, experiment_id, queries: [{query_id, query_hash, result_hash}]

Only the six fields spec v3 names for P4-K04 (query hash, result hash, q-value, effect size,
verified_by, stale_after) are required; everything else is optional so K04 can add ODCS/OpenLineage
references without breaking documents written here. Nothing in the document is ever executed.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from analystos.core.ids import utcnow
from analystos.knowledge import okf

TYPE = "Attested Computation"
KIND = "attested_computation"
STALE_AFTER_DAYS = 90
REQUIRED = ("query_hash", "result_hash", "q_value", "effect_size", "verified_by")
REV = "process:analystos-rev"


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


def computation_from(insight: Any, experiment: Any | None, queries: list[Any]) -> dict[str, Any]:
    """The computation block for a finding: its primary experiment's statistics and the queries it ran."""
    result = (experiment.result if experiment is not None else None) or {}
    qs = [{"query_id": q.id, "query_hash": sql_hash(q.executed_sql or q.sql), "result_hash": q.result_hash}
          for q in queries]
    return {"query_hash": qs[0]["query_hash"] if qs else None, "result_hash": qs[0]["result_hash"] if qs else None,
            "q_value": result.get("p_adjusted", result.get("p_value")),
            "effect_size": {"value": result.get("effect_size"), "label": result.get("effect_label")},
            "verified_by": REV if getattr(insight, "verified", False) else None,
            "method": result.get("test") or (experiment.method if experiment is not None else None), "n": result.get("n"),
            "run_id": insight.run_id, "insight_id": insight.id,
            "experiment_id": experiment.id if experiment is not None else None, "queries": qs}


def missing(computation: dict[str, Any]) -> list[str]:
    """Required fields that are absent: a draft without them cannot be approved."""
    out = [k for k in REQUIRED if computation.get(k) in (None, "", [])]
    if isinstance(computation.get("effect_size"), dict) and computation["effect_size"].get("value") is None:
        out.append("effect_size")
    return sorted(set(out))


def stale_after(now: datetime | None = None, days: int = STALE_AFTER_DAYS) -> str:
    return ((now or utcnow()) + timedelta(days=days)).replace(microsecond=0).isoformat()


def frontmatter(*, title: str, statement: str, computation: dict[str, Any], stale: str, status: str = "draft",
                verified: list[dict[str, Any]] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ext = {"kind": KIND, "computation": computation, "origin": "learning:finding", "trusted": status == "stable",
           **(extra or {})}
    fm: dict[str, Any] = {"type": TYPE, "title": title, "status": status, "stale_after": stale, "analystos": ext,
                          "description": okf_first_sentence(statement), "tags": ["attested-computation", "finding"]}
    if verified:
        fm["verified"] = verified
    return fm


def okf_first_sentence(text: str) -> str:
    from analystos.knowledge.entries import first_sentence

    return first_sentence(text) or text[:240]


def body(statement: str, computation: dict[str, Any]) -> str:
    eff = computation.get("effect_size") or {}
    lines = ["# Finding", "", statement.strip(), "", "# Computation", "",
             f"* method: {computation.get('method')} (n = {computation.get('n')})",
             f"* q-value (BH-adjusted): {computation.get('q_value')}",
             f"* effect size: {eff.get('value')} ({eff.get('label')})",
             f"* query sha256: {computation.get('query_hash')}",
             f"* result sha256: {computation.get('result_hash')}",
             f"* verified by: {computation.get('verified_by')}"]
    return "\n".join(lines)


def render(*, title: str, statement: str, computation: dict[str, Any], stale: str, status: str = "draft",
           verified: list[dict[str, Any]] | None = None, extra: dict[str, Any] | None = None) -> str:
    return okf.render_document(frontmatter(title=title, statement=statement, computation=computation, stale=stale,
                                           status=status, verified=verified, extra=extra), body(statement, computation))


def validate(doc: okf.OkfDocument) -> list[str]:
    """Problems with an Attested Computation document (empty when it has the minimal shape)."""
    if doc.type != TYPE:
        return [f"type is {doc.type!r}, not {TYPE!r}"]
    comp = doc.extension.get("computation")
    if not isinstance(comp, dict):
        return ["analystos.computation missing"]
    out = [f"computation.{k} missing" for k in missing(comp)]
    if okf.parse_instant(doc.frontmatter.get("stale_after")) is None:
        out.append("stale_after missing or not an instant")
    return out
