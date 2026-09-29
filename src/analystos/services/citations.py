"""Evidence citations of findings and Ask answers (N-8): the DB side of `evidence.fusion`.

Reads what the deterministic path recorded — a finding's typed facts and query receipts, an answer's
stored results, the context receipts of the model calls, the indexed knowledge sections — and writes
one `evidence_citation_set` row per subject, a `cites` lineage edge to every cited document, and the
`evidence.citations_recorded` / `evidence.conflict_flagged` events. Nothing here can make a number
verified: that stays with the numbers guard and REV.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.context.compiler import terms
from analystos.contracts.citations import Citations
from analystos.contracts.evidence import Fact
from analystos.evidence import fusion

log = logging.getLogger(__name__)


# ------------------------------------------------------------------------------ inputs
def _receipts(calls: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in calls:
        for r in c.context_receipts or []:
            if isinstance(r, dict) and r not in out:
                out.append(r)
    return out


def _sections(session: Session, receipts: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str | None], dict[str, Any]]:
    from analystos.db.models import KnowledgeSection

    ids = sorted({str(r["document_id"]) for r in receipts if r.get("document_id")})
    if not ids:
        return {}
    return {(s.document_id, s.anchor): {"text": s.text, "heading": s.heading}
            for s in session.scalars(select(KnowledgeSection).where(KnowledgeSection.document_id.in_(ids)))}


def _facts(bundle: Mapping[str, Any]) -> list[Fact]:
    out = []
    for f in (bundle.get("claim") or {}).get("facts") or []:
        try:
            out.append(Fact.model_validate(f))
        except ValueError:
            continue
    return out


def _spec_terms(spec: Mapping[str, Any], key: str) -> set[str]:
    d = spec.get(key) or {}
    return terms(f"{d.get('label') or ''} {str(d.get('column') or '').replace('_', ' ')}") if isinstance(d, dict) else set()


# ------------------------------------------------------------------------------ build
def insight_citations(session: Session, ins: Any, *, bundle: Mapping[str, Any] | None = None,
                      title: str | None = None, finding: str | None = None,
                      narrative_source: str | None = None) -> Citations:
    """A finding's citations: its governed query receipts and typed facts (quantitative), and the pack
    sections the run's model calls were grounded on that concern the finding's outcome (document) —
    every section the narrative call itself was given is kept."""
    from analystos.db.models import Hypothesis, ModelCall
    from analystos.evidence.verification import narrative_calls

    bundle = dict(bundle if bundle is not None else ins.evidence_bundle or {})
    facts = _facts(bundle)
    quantitative = fusion.quantitative_from_facts(facts, (bundle.get("data") or {}).get("queries") or [])
    calls = list(session.scalars(select(ModelCall).where(ModelCall.run_id == ins.run_id, ModelCall.workspace_id == ins.workspace_id,
                                                         ModelCall.status == "ok").order_by(ModelCall.id))) if ins.run_id else []
    receipts = _receipts(calls)
    source = narrative_source if narrative_source is not None else ins.narrative_source
    narrated = {f"d:{r.get('document_id')}#{r.get('anchor') or ''}" for r in _receipts(narrative_calls(session, ins.run_id, source))}
    h = session.get(Hypothesis, ins.hypothesis_id) if ins.hypothesis_id else None
    spec = dict(h.spec or {}) if h is not None else {}
    topic = _spec_terms(spec, "outcome") or terms(str((bundle.get("claim") or {}).get("metric") or ""))
    docs = [d for d in fusion.document_citations(receipts, _sections(session, receipts))
            if d.id in narrated or (topic and topic & terms(f"{d.heading or ''} {d.excerpt}"))]
    labels = [str(x) for f in facts for x in (f.subject, f.baseline, f.metric, f.dimension, f.window) if x]
    text = f"{title if title is not None else ins.title}. {finding if finding is not None else ins.finding}"
    return fusion.fuse("insight", ins.id, text=text, quantitative=quantitative, documents=docs,
                       measured=fusion.measured_from_facts(facts), labels=labels)


def _turn_results(turn: Any) -> list[dict[str, Any]]:
    analysis = turn.analysis if isinstance(turn.analysis, dict) else None
    if analysis:
        return [{**(s.get("result") or {}), "step": s.get("n")} for s in analysis.get("steps") or []
                if s.get("status") == "answered" and isinstance(s.get("result"), dict)]
    return [dict(turn.result)] if isinstance(turn.result, dict) else []


def turn_citations(session: Session, turn: Any) -> Citations:
    """An Ask answer's citations: each stored query result (quantitative) and the pack sections the
    turn's own model calls were given (document)."""
    from analystos.db.models import ModelCall

    calls = list(session.scalars(select(ModelCall).where(ModelCall.task_id == turn.id, ModelCall.workspace_id == turn.workspace_id)
                                 .order_by(ModelCall.id)))
    receipts = _receipts(calls)
    results = _turn_results(turn)
    analysis = turn.analysis if isinstance(turn.analysis, dict) else {}
    text = str((analysis.get("synthesis") or {}).get("text") or turn.explanation or "")
    labels = [turn.question or ""] + [str(c) for r in results for c in r.get("columns") or []]
    labels += [v for r in results for row in r.get("rows") or [] if isinstance(row, list | tuple) for v in row if isinstance(v, str)]
    return fusion.fuse("ask_turn", turn.id, text=text, quantitative=fusion.quantitative_from_results(results),
                       documents=fusion.document_citations(receipts, _sections(session, receipts)),
                       measured=fusion.measured_from_results(results), labels=labels)


# ------------------------------------------------------------------------------ persist / read
def record(session: Session, cit: Citations, *, workspace_id: str, run_id: str | None = None,
           actor: str | None = None) -> Any:
    """Write (or rewrite) the subject's citation set, its `cites` edges and events."""
    from analystos.artifacts.registry import link
    from analystos.core.ids import new_id, utcnow
    from analystos.db.models import EvidenceCitationSet
    from analystos.events.bus import emit

    row = session.scalar(select(EvidenceCitationSet).where(EvidenceCitationSet.subject_type == cit.subject_type,
                                                           EvidenceCitationSet.subject_id == cit.subject_id))
    if row is None:
        row = EvidenceCitationSet(id=new_id("cit"), workspace_id=workspace_id, subject_type=cit.subject_type,
                                  subject_id=cit.subject_id)
        session.add(row)
    doc_ids = sorted({d.document_id for d in cit.documents})
    row.run_id, row.version = run_id, cit.version
    row.quantitative_count, row.document_count, row.conflict_count = len(cit.quantitative), len(cit.documents), len(cit.conflicts)
    row.document_ids, row.citations, row.updated_at = doc_ids, cit.model_dump(mode="json"), utcnow()
    session.flush()
    for d in doc_ids:
        link(session, workspace_id, (cit.subject_type, cit.subject_id), "cites", ("knowledge_document", d), run_id=run_id)
    subject = {"subject_type": cit.subject_type, "subject_id": cit.subject_id}
    emit(workspace_id, "evidence.citations_recorded", {**subject, **cit.summary()}, run_id=run_id, actor=actor, session=session)
    for c in cit.conflicts:
        emit(workspace_id, "evidence.conflict_flagged",
             {**subject, "path": c.path, "anchor": c.anchor, "metric": c.metric, "subject": c.subject,
              "document_text": c.document_text, "measured_value": c.measured_value, "unit": c.unit},
             run_id=run_id, actor=actor, session=session)
    return row


def record_turn_quietly(session: Session, turn: Any, *, actor: str | None = None) -> None:
    """Citations of an answered turn; a failure is logged and never fails the answer."""
    try:
        with session.begin_nested():
            record(session, turn_citations(session, turn), workspace_id=turn.workspace_id, actor=actor)
    except Exception:  # noqa: BLE001 - citations describe an answer; they must not lose it
        log.exception("could not record citations of ask turn %s", turn.id)


def view(cit: Citations | Mapping[str, Any], *, recorded: bool) -> dict[str, Any]:
    c = cit if isinstance(cit, Citations) else Citations.model_validate(cit)
    return {**c.model_dump(mode="json"), "summary": c.summary(), "recorded": recorded}


def stored(session: Session, subject_type: str, subject_id: str) -> dict[str, Any] | None:
    from analystos.db.models import EvidenceCitationSet

    row = session.scalar(select(EvidenceCitationSet).where(EvidenceCitationSet.subject_type == subject_type,
                                                           EvidenceCitationSet.subject_id == subject_id))
    return view(row.citations, recorded=True) if row is not None and row.citations else None


def for_insight(session: Session, ins: Any) -> dict[str, Any]:
    """The recorded citation set, or (a finding verified before N-8) one computed now and marked unrecorded."""
    return stored(session, "insight", ins.id) or view(insight_citations(session, ins), recorded=False)


def for_turn(session: Session, turn: Any) -> dict[str, Any]:
    return stored(session, "ask_turn", turn.id) or view(turn_citations(session, turn), recorded=False)


def conflicts(session: Session, workspace_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Findings and answers of a workspace whose cited documents disagree with measured data."""
    from analystos.db.models import EvidenceCitationSet

    rows = session.scalars(select(EvidenceCitationSet).where(EvidenceCitationSet.workspace_id == workspace_id,
                                                             EvidenceCitationSet.conflict_count > 0)
                           .order_by(EvidenceCitationSet.updated_at.desc()).limit(limit))
    return [{"subject_type": r.subject_type, "subject_id": r.subject_id, "run_id": r.run_id,
             "conflicts": (r.citations or {}).get("conflicts") or [], "updated_at": r.updated_at} for r in rows]


__all__ = ["conflicts", "for_insight", "for_turn", "insight_citations", "record", "record_turn_quietly", "stored",
           "turn_citations", "view"]
