"""The knowledge review queue (P4-K07, P4-K08): drafts in, pack revisions out.

Models propose, code decides. A draft — from crawler enrichment (a model) or from the learning loop
(`knowledge/learning.py`: an accepted finding, an approved KPI, a user correction) — is a
`knowledge_suggestion` row whose every field carries `{value, confidence, provenance}`. Drafts never
reach a prompt. A reviewer (workspace editor) decides them in batches:

* **approve** / **edit** (edit-then-approve: the edited fields become `provenance.source = human`,
  confidence 1.0) — the document is written into the workspace pack; one revision per batch;
* **reject** — a *negative knowledge* document ("reviewed and rejected: …, reason") is written into
  the pack instead, which later prompts see in their `negative_knowledge` section; the same content
  is never proposed again for that subject, and the crawler tells the model what was rejected.

Invariants (CLAUDE.md, crawler): a draft never overwrites a document the queue did not write (owner
or imported content): proposing is skipped and approving is refused. A catalog description is
changed only while it is still a model or rule placeholder: reviewed, user and source-system text
wins. A draft for a subject supersedes that subject's older pending drafts.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput
from analystos.core.ids import new_id, utcnow
from analystos.db.models import KnowledgeDocument, KnowledgeSuggestion, SourceAsset
from analystos.knowledge import attested, okf, store
from analystos.knowledge.entries import KIND_HEADINGS, first_sentence, slugify

# kind -> (OKF type, pack directory, `analystos.kind` of the written document)
KIND_SPEC: dict[str, tuple[str, str, str]] = {
    "term": ("Glossary Term", "glossary", "term"),
    "definition": ("Definition", "glossary", "definition"),
    "metric": ("Metric", "metrics", "metric"),
    "rule": ("Business Rule", "rules", "rule"),
    "note": ("Note", "notes", "note"),
    "negative": ("Negative Knowledge", "negative", "negative"),
    "attested_computation": (attested.TYPE, "findings", attested.KIND),
    "table_description": ("Table", "catalog", "table"),
}
PRIMARY_FIELD = {"attested_computation": "statement", "table_description": "description"}
STATUSES = ("pending", "approved", "rejected", "superseded")
ACTIONS = ("approve", "edit", "reject")
REVIEW_KEY = "review"  # `analystos.review` marks a document the queue wrote (and may replace)


def primary_field(kind: str) -> str:
    return PRIMARY_FIELD.get(kind, "body")


def field(value: Any, confidence: float, **provenance: Any) -> dict[str, Any]:
    """One field of a draft. Confidence is advisory and clamped to [0, 1]."""
    return {"value": value, "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
            "provenance": {k: v for k, v in provenance.items() if v is not None}}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def content_hash(kind: str, subject: str, title: str, fields: dict[str, Any]) -> str:
    material = {"kind": kind, "subject": subject, "title": title,
                "fields": {k: (v or {}).get("value") for k, v in sorted(fields.items())}}
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def default_path(kind: str, title: str, subject: str) -> str:
    folder = KIND_SPEC[kind][1]
    if kind in ("attested_computation", "table_description"):
        return f"{folder}/{slugify(subject.split(':', 1)[-1], 120)}.md"
    return f"{folder}/{slugify(title)}.md"


def _subject_ref(subject: str) -> tuple[str, str] | None:
    kind, _, ref = subject.partition(":")
    return (kind, ref) if kind and ref else None


# ------------------------------------------------------------------------------------ protection
def _indexed_doc(session: Session, workspace_id: str, path: str) -> KnowledgeDocument | None:
    from analystos.knowledge.index import doc_id

    pack = store.workspace_pack(session, workspace_id, create=False)
    return session.get(KnowledgeDocument, doc_id(pack.id, path)) if pack is not None else None


def protected(session: Session, workspace_id: str, path: str) -> bool:
    """A document exists at `path` that the review queue did not write: owner content, never replaced."""
    d = _indexed_doc(session, workspace_id, path)
    if d is None:
        return False
    ext = (d.frontmatter or {}).get("analystos")
    return not (isinstance(ext, dict) and isinstance(ext.get(REVIEW_KEY), dict))


def rejected_values(session: Session, workspace_id: str, subject: str, name: str | None = None) -> set[str]:
    """Normalised values of a subject's rejected drafts (for `name`, default each draft's primary
    field): what the suggester must not propose again."""
    out = set()
    for r in session.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == workspace_id,
                                                               KnowledgeSuggestion.subject == subject,
                                                               KnowledgeSuggestion.status == "rejected")):
        f = (r.fields or {}).get(name or primary_field(r.kind)) or {}
        if f.get("value"):
            out.add(_norm(f["value"]))
    return out


# ------------------------------------------------------------------------------------ propose
def propose(session: Session, workspace_id: str, *, kind: str, subject: str, title: str, fields: dict[str, Any],
            origin: str, proposed_by: str, batch: str | None = None, path: str | None = None) -> KnowledgeSuggestion | None:
    """Queue a draft. Returns the (new or identical pending) draft, or None when it is skipped: the
    same content was already decided, its primary value was rejected for this subject, or owner
    content exists at its path."""
    from analystos.artifacts.registry import link
    from analystos.events.bus import emit

    if kind not in KIND_SPEC:
        raise InvalidInput(f"unknown knowledge kind {kind!r} ({', '.join(KIND_SPEC)})")
    shaped = {k: (v if isinstance(v, dict) and "value" in v else field(v, 0.0, source="unknown")) for k, v in fields.items()}
    main = primary_field(kind)
    if not str((shaped.get(main) or {}).get("value") or "").strip():
        raise InvalidInput(f"a {kind} draft needs a non-empty `{main}`")
    title = " ".join(str(title).split())[:300] or "untitled"
    path = path or default_path(kind, title, subject)
    digest = content_hash(kind, subject, title, shaped)
    same = session.scalar(select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == workspace_id,
                                                            KnowledgeSuggestion.subject == subject,
                                                            KnowledgeSuggestion.content_hash == digest))
    if same is not None:
        return same if same.status == "pending" else None
    if _norm(shaped[main]["value"]) in rejected_values(session, workspace_id, subject, main) or protected(session, workspace_id, path):
        return None
    for old in session.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == workspace_id,
                                                                  KnowledgeSuggestion.subject == subject,
                                                                  KnowledgeSuggestion.status == "pending")):
        old.status, old.reason = "superseded", "a newer draft for the same subject"
    row = KnowledgeSuggestion(id=new_id("ksug"), workspace_id=workspace_id, kind=kind, subject=subject[:200], title=title,
                              path=path, fields=shaped, confidence=min(float(v.get("confidence") or 0.0) for v in shaped.values()),
                              origin=origin[:60], proposed_by=proposed_by[:80], batch=batch, status="pending",
                              content_hash=digest)
    session.add(row)
    session.flush()
    ref = _subject_ref(subject)
    if ref:
        link(session, workspace_id, ("knowledge_suggestion", row.id), "derived_from", ref)
    emit(workspace_id, "knowledge.suggestion_proposed", {"id": row.id, "kind": kind, "subject": subject, "title": title,
                                                         "origin": origin, "confidence": row.confidence},
         actor=proposed_by, session=session)
    return row


# ------------------------------------------------------------------------------------ queue
def as_dict(row: KnowledgeSuggestion) -> dict[str, Any]:
    return {"id": row.id, "kind": row.kind, "subject": row.subject, "title": row.title, "path": row.path,
            "fields": row.fields, "confidence": row.confidence, "origin": row.origin, "proposed_by": row.proposed_by,
            "batch": row.batch, "status": row.status, "decided_by": row.decided_by, "decided_at": row.decided_at,
            "reason": row.reason, "revision": row.revision, "created_at": row.created_at}


def queue(session: Session, workspace_id: str, *, status: str | None = "pending", kind: str | None = None,
          origin: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    q = select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == workspace_id)
    if status:
        if status not in STATUSES:
            raise InvalidInput(f"status must be one of {STATUSES}")
        q = q.where(KnowledgeSuggestion.status == status)
    if kind:
        q = q.where(KnowledgeSuggestion.kind == kind)
    if origin:
        q = q.where(KnowledgeSuggestion.origin == origin)
    rows = session.scalars(q.order_by(KnowledgeSuggestion.created_at, KnowledgeSuggestion.id).limit(max(1, min(limit, 500))))
    return [as_dict(r) for r in rows]


# ------------------------------------------------------------------------------------ rendering
def _value(fields: dict[str, Any], name: str, default: Any = None) -> Any:
    f = fields.get(name)
    return f.get("value", default) if isinstance(f, dict) else default


def _review_block(row: KnowledgeSuggestion, fields: dict[str, Any], user_id: str, decision: str) -> dict[str, Any]:
    return {"suggestion_id": row.id, "subject": row.subject, "origin": row.origin, "proposed_by": row.proposed_by,
            "decision": decision, "decided_by": f"user:{user_id}", "confidence": row.confidence,
            "fields": {k: {"confidence": v.get("confidence"), "provenance": v.get("provenance")} for k, v in sorted(fields.items())}}


def render(row: KnowledgeSuggestion, fields: dict[str, Any], user_id: str, at: str) -> bytes:
    """The approved document: OKF v0.2, human-reviewed (§5.2 `verified`), provenance under `analystos.review`."""
    type_, _, doc_kind = KIND_SPEC[row.kind]
    human = {"by": f"human:{user_id}", "at": at}
    review = _review_block(row, fields, user_id, "approved")
    if row.kind == "attested_computation":
        comp = dict(_value(fields, "computation") or {})
        verified = ([{"by": comp["verified_by"], "at": at}] if comp.get("verified_by") else []) + [human]
        return attested.render(title=row.title, statement=str(_value(fields, "statement")), computation=comp,
                               stale=str(_value(fields, "stale_after") or attested.stale_after()), status="stable",
                               verified=verified, extra={REVIEW_KEY: review, "trusted": True}).encode()
    text = str(_value(fields, primary_field(row.kind)) or "")
    ext: dict[str, Any] = {"kind": doc_kind, "origin": f"review:{row.origin}", "trusted": True, REVIEW_KEY: review}
    for name in ("synonyms", "mapped_columns"):
        v = _value(fields, name)
        if isinstance(v, list) and v:
            ext[name] = [str(x) for x in v]
    fm: dict[str, Any] = {"type": type_, "title": row.title, "status": "stable", "analystos": ext, "verified": [human],
                          "generated": {"by": row.proposed_by}, "tags": sorted({doc_kind, "reviewed"})}
    if first_sentence(text):
        fm["description"] = first_sentence(text)
    if row.kind == "table_description":
        heading = "Description"
        if _value(fields, "business_name"):
            ext["business_name"] = str(_value(fields, "business_name"))
    else:
        heading = KIND_HEADINGS.get(doc_kind, "Note" if doc_kind == "note" else "Definition")
    return okf.render_document(fm, f"# {heading}\n\n{text.strip()}").encode()


def negative_path(row: KnowledgeSuggestion) -> str:
    return f"negative/{slugify(row.kind)}-{slugify(row.subject.split(':', 1)[-1] or row.title, 80)}-{row.content_hash[:8]}.md"


def render_negative(row: KnowledgeSuggestion, user_id: str, at: str, reason: str | None) -> bytes:
    """A rejection as knowledge: what was proposed, that a human rejected it, and why."""
    text = str(_value(row.fields or {}, primary_field(row.kind)) or "")
    ext = {"kind": "negative", "origin": f"review:{row.origin}", "trusted": True,
           REVIEW_KEY: _review_block(row, row.fields or {}, user_id, "rejected"), "rejected_kind": row.kind}
    fm = {"type": "Negative Knowledge", "title": f"Rejected: {row.title}"[:300], "status": "stable", "analystos": ext,
          "verified": [{"by": f"human:{user_id}", "at": at}], "tags": ["negative", slugify(row.kind)],
          "description": f"A reviewer rejected this {row.kind.replace('_', ' ')}: do not rely on it."}
    body = (f"# Rejected\n\nA proposed {row.kind.replace('_', ' ')} for {row.subject} was reviewed and rejected; "
            f"it is not accepted knowledge. Proposed text: {text.strip()}"
            + (f"\n\n# Reason\n\n{reason.strip()}" if reason and reason.strip() else ""))
    return okf.render_document(fm, body).encode()


# ------------------------------------------------------------------------------------ catalog side
def _asset(session: Session, row: KnowledgeSuggestion) -> SourceAsset | None:
    ref = _subject_ref(row.subject)
    if not ref or ref[0] != "asset":
        return None
    a = session.get(SourceAsset, ref[1])
    return a if a is not None and a.workspace_id == row.workspace_id else None


def _apply_description(session: Session, row: KnowledgeSuggestion, fields: dict[str, Any]) -> str:
    a = _asset(session, row)
    if a is None:
        return "asset not found: pack only"
    text = str(_value(fields, "description") or "")
    if (a.reviewed or a.description_origin in ("user", "source")) and _norm(a.description) != _norm(text):
        return "catalog kept: reviewed or owner description"
    edited = ((fields.get("description") or {}).get("provenance") or {}).get("source") == "human"
    a.description, a.description_origin, a.reviewed = text, "user" if edited else "model", True
    bn = _value(fields, "business_name")
    if bn and a.business_name_origin in (None, "rule", "model"):
        a.business_name, a.business_name_origin = str(bn)[:300], "model"
    return "catalog updated"


def _revert_description(session: Session, row: KnowledgeSuggestion) -> str:
    a = _asset(session, row)
    if a is None:
        return "asset not found"
    f = (row.fields or {}).get("description") or {}
    if a.reviewed or a.description_origin != "model" or _norm(a.description) != _norm(f.get("value")):
        return "catalog unchanged"
    before = f.get("before") or {}
    a.description, a.description_origin = before.get("value"), before.get("origin")
    return "catalog restored"


# ------------------------------------------------------------------------------------ review
def review(session: Session, workspace_id: str, user: Any, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide a batch. Every accepted decision lands in ONE workspace-pack revision; a decision that
    cannot apply (not pending, not found, owner content at the path, an attested computation
    missing required fields) is reported in `errors` and changes nothing."""
    from analystos.events.bus import emit
    from analystos.governance.audit import audit

    if not decisions:
        raise InvalidInput("no decisions")
    at = utcnow().replace(microsecond=0).isoformat()
    files: dict[str, bytes] = {}
    decided: list[tuple[KnowledgeSuggestion, str, str | None]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in decisions:
        sid, action = str(d.get("id") or ""), str(d.get("action") or "")
        row = session.get(KnowledgeSuggestion, sid)
        if row is None or row.workspace_id != workspace_id:
            errors.append({"id": sid, "error": "not_found"})
            continue
        if sid in seen or row.status != "pending":
            errors.append({"id": sid, "error": "not_pending", "status": row.status})
            continue
        if action not in ACTIONS:
            errors.append({"id": sid, "error": "invalid_action"})
            continue
        seen.add(sid)
        if action == "reject":
            if row.kind != "negative":  # rejecting a negative draft just drops it
                files[negative_path(row)] = render_negative(row, user.id, at, d.get("reason"))
            decided.append((row, "rejected", _revert_description(session, row) if row.kind == "table_description" else None))
            row.reason = str(d["reason"])[:2000] if d.get("reason") else None
            continue
        fields = dict(row.fields or {})
        for name, value in (d.get("fields") or {}).items() if action == "edit" else ():
            old = fields.get(name) or {}
            fields[name] = {**field(value, 1.0, source="human", by=f"user:{user.id}",
                                    edited_from=old.get("provenance")), **({"before": old["before"]} if "before" in old else {})}
        if not str(_value(fields, primary_field(row.kind)) or "").strip():
            errors.append({"id": sid, "error": "empty", "field": primary_field(row.kind)})
            continue
        if row.kind == "attested_computation" and attested.missing(_value(fields, "computation") or {}):
            errors.append({"id": sid, "error": "incomplete_computation", "missing": attested.missing(_value(fields, "computation") or {})})
            continue
        if row.path not in files and protected(session, workspace_id, row.path):
            errors.append({"id": sid, "error": "owner_content_exists", "path": row.path})
            continue
        files[row.path] = render(row, fields, user.id, at)
        note = _apply_description(session, row, fields) if row.kind == "table_description" else None
        if action == "edit":
            row.fields = fields
        decided.append((row, "approved", note))
    revision = None
    if files:
        pack = store.workspace_pack(session, workspace_id)
        approved = sum(1 for _, s, _ in decided if s == "approved")
        store.commit(session, pack, files, author=f"user:{user.id}", origin="review", merge=True,
                     reason=f"knowledge review: {approved} approved, {len(decided) - approved} rejected",
                     meta={"suggestions": [r.id for r, _, _ in decided]})
        revision = pack.head_revision
    now = utcnow()
    out: dict[str, list[dict[str, Any]]] = {"approved": [], "rejected": []}
    for row, status, note in decided:
        row.status, row.decided_by, row.decided_at, row.revision = status, user.id, now, revision
        path = row.path if status == "approved" else (negative_path(row) if row.kind != "negative" else None)
        out[status].append({"id": row.id, "path": path,
                            **({"catalog": note} if note else {})})
        audit(f"user:{user.id}", f"knowledge.suggestion.{status}", workspace_id=workspace_id, target=row.id,
              decision="allow" if status == "approved" else "deny", details={"kind": row.kind, "subject": row.subject,
                                                                            "revision": revision}, session=session)
    if decided:
        emit(workspace_id, "knowledge.suggestions_reviewed", {"approved": len(out["approved"]), "rejected": len(out["rejected"]),
                                                              "revision": revision}, actor=f"user:{user.id}", session=session)
    return {"revision": revision, **out, "errors": errors}
