"""The learning loop (P4-K08): what people approve or correct becomes a knowledge *draft* in the
review queue (knowledge/suggestions.py) — never knowledge by itself.

* an accepted finding (P4-T09 `finding.accept`)       -> a draft `Attested Computation`
* an approved KPI (semantic layer, SEM-002)          -> a draft `Metric` document
* user corrections and redirects (run feedback):
  `add_context` -> a `Note`; `redirect`/`deeper_analysis` -> a `Note` on the analysts' focus;
  `reject_finding` -> a `Negative Knowledge` draft about the rejected claim

Everything here is deterministic code over persisted rows: the draft's fields quote the source
row and carry provenance to it (and a lineage edge). Drafting is best effort — it runs inside the
caller's transaction under a savepoint and never fails the action that triggered it.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.logging import get_logger
from analystos.knowledge import attested
from analystos.knowledge.suggestions import field, propose

log = get_logger(__name__)
PROCESS = "process:analystos-learning"


def _safely(session: Session, what: str, fn: Any) -> Any:
    try:
        with session.begin_nested():
            return fn()
    except Exception as exc:  # noqa: BLE001 - the triggering action must not fail on a draft
        log.warning("learning loop: %s draft skipped: %s", what, exc)
        return None


# ------------------------------------------------------------------------------------ findings
def draft_from_finding(session: Session, insight: Any, *, user_id: str | None) -> Any:
    """An accepted finding -> a draft Attested Computation (query hash, result hash, q-value,
    effect size, verified_by, stale_after). Only verified findings are drafted."""
    from analystos.db.models import Experiment, QueryExecution

    def run() -> Any:
        if insight.status != "verified" or getattr(insight, "stale_since", None) is not None:
            return None  # a stale finding (P4-03: its snapshot changed) is re-verified before promotion
        exp = None
        if insight.hypothesis_id:
            exp = session.scalar(select(Experiment).where(Experiment.hypothesis_id == insight.hypothesis_id,
                                                          Experiment.run_id == insight.run_id, Experiment.role == "primary")
                                 .order_by(Experiment.created_at.desc()).limit(1))
        ids = list((exp.query_ids if exp is not None else None) or [])
        queries = {q.id: q for q in session.scalars(select(QueryExecution).where(QueryExecution.id.in_(ids),
                                                                                  QueryExecution.workspace_id == insight.workspace_id))} if ids else {}
        comp = attested.computation_from(insight, exp, [queries[i] for i in ids if i in queries])
        prov = {"source": "insight", "insight_id": insight.id, "run_id": insight.run_id}
        fields = {
            "statement": field(insight.finding, insight.confidence or 0.0, **prov, accepted_by=f"user:{user_id}" if user_id else None),
            "computation": field(comp, 1.0 if not attested.missing(comp) else 0.0, source="rev",
                                 experiment_id=comp.get("experiment_id"), query_ids=[q["query_id"] for q in comp["queries"]]),
            "stale_after": field(attested.stale_after(), 1.0, source="policy", days=attested.STALE_AFTER_DAYS),
        }
        return propose(session, insight.workspace_id, kind="attested_computation", subject=f"insight:{insight.id}",
                       title=insight.title, fields=fields, origin="learning.finding", proposed_by=PROCESS,
                       batch=insight.run_id)

    return _safely(session, "finding", run)


# ------------------------------------------------------------------------------------ KPIs
def draft_from_metric(session: Session, row: Any) -> Any:
    """An approved semantic metric -> a draft Metric document for the workspace pack."""
    def run() -> Any:
        d = row.definition or {}
        ai = d.get("ai_context")
        description = str(d.get("description") or (ai if isinstance(ai, str) else "") or "").strip()
        body = "\n\n".join(x for x in (description, f"Expression: `{row.expression}`") if x)
        synonyms = [str(s) for s in ((ai.get("synonyms") if isinstance(ai, dict) else None) or []) if isinstance(s, str)]
        columns = [str(c) for c in d.get("source_columns") or [] if isinstance(c, str)]
        prov = {"source": "semantic_metric", "metric_id": row.id, "version": row.version, "approval_id": row.approval_id}
        fields = {"body": field(body, 1.0, **prov)}
        if synonyms:
            fields["synonyms"] = field(synonyms, 1.0, **prov)
        if columns:
            fields["mapped_columns"] = field(columns, 1.0, **prov)
        return propose(session, row.workspace_id, kind="metric", subject=f"metric:{row.name}@v{row.version}",
                       title=row.display_name or row.name, fields=fields, origin="learning.metric", proposed_by=PROCESS,
                       path=f"metrics/{_slug(row.name)}.md")

    return _safely(session, "metric", run)


def _slug(text: str) -> str:
    from analystos.knowledge.entries import slugify

    return slugify(text)


# ------------------------------------------------------------------------------------ feedback
def draft_from_feedback(session: Session, fb: Any, *, objective: str = "", target: Any = None) -> Any:
    """A user correction or redirect -> a Note (context, focus) or Negative Knowledge draft."""
    def run() -> Any:
        text = " ".join((fb.text or "").split())
        if not text:
            return None
        prov = {"source": "feedback", "feedback_id": fb.id, "run_id": fb.run_id, "by": f"user:{fb.user_id}", "kind": fb.kind}
        if fb.kind == "add_context":
            kind, title, body = "note", f"User context: {text[:80]}", text
        elif fb.kind in ("redirect", "deeper_analysis"):
            interp = (fb.data or {}).get("interpretation") or {}
            filters = "; ".join(f"{f.get('column')} {f.get('op')} {f.get('value')}" for f in interp.get("filters") or []
                                if isinstance(f, dict))
            kind, title = "note", f"Analyst focus: {text[:80]}"
            body = (f"When analysing \"{objective[:200]}\", an analyst asked: {text}"
                    + (f" (interpreted as filters: {filters})." if filters else "."))
        elif fb.kind == "reject_finding" and target is not None:
            kind, title = "negative", f"Rejected finding: {target.title}"
            body = f"The finding \"{target.title}\" ({target.finding}) was rejected by a user: {text}"
        else:
            return None
        return propose(session, fb.workspace_id, kind=kind, subject=f"feedback:{fb.id}", title=title,
                       fields={"body": field(body, 0.9 if kind != "note" or fb.kind == "add_context" else 0.6, **prov)},
                       origin="learning.feedback", proposed_by=PROCESS, batch=fb.run_id)

    return _safely(session, "feedback", run)
