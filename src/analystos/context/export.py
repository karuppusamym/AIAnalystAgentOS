"""Workspace context export (Stream D): everything the platform knows *about* a workspace's data, as one
download to investigate or carry elsewhere — the whole workspace or one integration (source).

`collect` is the only database access; it builds a plain, ordered snapshot. The renderers are pure:

* `render_json`      one versioned document (`analystos.context/v1`) with a digest of its content;
* `render_markdown`  one readable file;
* `render_okf`       an OKF v0.2 bundle in the crawler's layout (`knowledge/crawl_docs.py`: sources/, tables/)
                     plus glossary/, metrics/, model/, contexts/, brief.md and index.md. It re-imports through
                     `POST /knowledge/import` as a read-only pack.

Deterministic and model-free. Never included: secrets, `secret_ref`, connection settings, error texts and raw
rows. Profiles are the catalog's: a sensitive, restricted or PII column keeps completeness and cardinality only
(`api/routers/catalog.public_profile`), and value lists (top values, enumerations, text min/max) are left out
unless the workspace policy lets data samples reach models (`send_data_samples_to_models`)."""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, InvalidInput
from analystos.core.ids import stable_hash
from analystos.core.logging import get_logger
from analystos.knowledge.entries import visible_entries

log = get_logger(__name__)

FORMAT = "analystos.context/v1"
FORMATS: dict[str, tuple[str, str]] = {"okf": ("application/zip", "okf.zip"), "json": ("application/json", "json"),
                                       "markdown": ("text/markdown; charset=utf-8", "md")}
GENERATOR = "process:analystos-context-export"
NEVER_INCLUDED = ("secrets and secret references", "connection settings", "raw rows")
_VALUE_KEYS = frozenset({"top_values", "values", "values_complete"})
_RANGED = frozenset({"numeric", "datetime"})
_DOC_LIMIT = 240 * 1024  # under the OKF publish/import cap per document (256 KiB)


# ------------------------------------------------------------------------------------ helpers
def _iso(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _clean(value: Any) -> Any:
    """JSON-safe, deterministic: datetimes as ISO text, dict keys sorted."""
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return _iso(value)


def slug(text: str, limit: int = 60) -> str:
    from analystos.knowledge.entries import slugify

    return slugify(text or "workspace", limit)


def export_profile(profile: dict | None, tags: list[str] | None, semantics: dict | None, *,
                   samples_allowed: bool) -> dict | None:
    """The column profile an export may carry: the catalog's public profile (sensitive columns reduced to
    completeness and cardinality), without value lists unless the policy lets data samples leave."""
    from analystos.api.routers.catalog import public_profile

    out = public_profile(profile, tags, semantics)
    if not out or samples_allowed:
        return out
    out = {k: v for k, v in out.items() if k not in _VALUE_KEYS}
    sem = out.get("semantic_type") or (out.get("type_family") if out.get("type_family") in _RANGED else None)
    if sem not in _RANGED:  # a text or id min/max is a value
        out.pop("min", None)
        out.pop("max", None)
    return out


def _in_tables(ref: str | None, tables: set[str]) -> bool:
    """`schema.table[.column]` belongs to one of `tables` (lower-case fq names)."""
    if not ref:
        return False
    r = str(ref).lower()
    return r in tables or r.rsplit(".", 1)[0] in tables


# ------------------------------------------------------------------------------------ collect
def collect(session: Session, workspace_id: str, *, source_id: str | None = None) -> dict[str, Any]:
    """The workspace's context as plain data, in a stable order. With `source_id`, only that integration's assets
    and what references them (glossary entries, metrics, relationships, model datasets, brief assertions,
    documents and analysis contexts that name one of its tables or the source)."""
    from analystos.db.models import (
        ContextEntry,
        CrawlRun,
        Definition,
        KnowledgeDocument,
        KnowledgePack,
        Relationship,
        SemanticMetric,
        SemanticModel,
        SemanticRelationshipCandidate,
        Source,
        SourceAsset,
        SourceColumn,
        User,
    )
    from analystos.governance.policy import get_workspace, load_policy
    from analystos.semantic import suggest as suggest_mod
    from analystos.services import brief as brief_svc

    ws = get_workspace(session, workspace_id)
    policy = load_policy(session, ws)
    samples = bool(policy.send_data_samples_to_models)

    sources = list(session.scalars(select(Source).where(Source.workspace_id == workspace_id)
                                   .order_by(Source.name, Source.id)))
    if source_id:
        sources = [s for s in sources if s.id == source_id]
        if not sources:
            raise InvalidInput(f"source {source_id} is not in this workspace")
    source_ids = {s.id for s in sources}
    names = {s.id: s.name for s in sources}

    last_crawl: dict[str, Any] = {}
    if source_ids:
        for r in session.scalars(select(CrawlRun).where(CrawlRun.workspace_id == workspace_id, CrawlRun.source_id.in_(source_ids))
                                 .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())):
            last_crawl.setdefault(r.source_id, r)

    stmt = select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.lifecycle == "active")
    if source_id:
        stmt = stmt.where(SourceAsset.source_id == source_id)
    assets = list(session.scalars(stmt.order_by(SourceAsset.schema_name, SourceAsset.name, SourceAsset.id)))
    asset_ids = {a.id for a in assets}
    fq_of = {a.id: f"{a.schema_name}.{a.name}" for a in assets}
    tables = {v.lower() for v in fq_of.values()}
    cols_by: dict[str, list[Any]] = defaultdict(list)
    if assets:
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(asset_ids))
                                 .order_by(SourceColumn.asset_id, SourceColumn.ordinal, SourceColumn.id)):
            cols_by[c.asset_id].append(c)

    brief_row = brief_svc.head(session, workspace_id)
    assertions = brief_svc.assertions_of(brief_row)
    event_time = {a.subject: a.value for a in assertions if a.effective and a.group == "time_measures"
                  and a.field == "event_time"}
    try:
        approved_keys = suggest_mod._approved_keys(session, workspace_id)
    except Exception:  # the key evidence is a convenience; an unreadable model must not stop an export
        approved_keys = {}

    asset_rows = []
    for a in assets:
        sem = a.semantics or {}
        fq = fq_of[a.id]
        cols = cols_by[a.id]
        key = suggest_mod.primary_key(a, cols, approved_keys.get(fq.lower()))
        asset_rows.append({
            "id": a.id, "fq": fq, "source_id": a.source_id, "source": names.get(a.source_id), "schema_name": a.schema_name,
            "name": a.name, "kind": a.kind, "selected": bool(a.selected), "role": sem.get("role"), "domain": sem.get("domain"),
            "grain": sem.get("grain"), "entity": sem.get("entity"), "confidence": sem.get("confidence"),
            "row_count": a.row_count, "key": key, "time_column": event_time.get(fq) or suggest_mod._time_column(cols),
            "business_name": a.business_name, "business_name_origin": a.business_name_origin,
            "description": a.description, "description_origin": a.description_origin, "reviewed": bool(a.reviewed),
            "last_crawled_at": a.last_crawled_at, "profile_meta": (a.stats or {}).get("profile_meta"),
            "columns": [_column(c, samples_allowed=samples) for c in cols]})

    rel_rows = []
    for r in session.scalars(select(Relationship).where(Relationship.workspace_id == workspace_id).order_by(Relationship.id)):
        if source_id and not ({r.from_asset_id, r.to_asset_id} & asset_ids):
            continue
        rel_rows.append({"id": r.id, "from": f"{fq_of.get(r.from_asset_id, r.from_asset_id)}.{r.from_column}",
                         "to": f"{fq_of.get(r.to_asset_id, r.to_asset_id)}.{r.to_column}", "cardinality": r.cardinality,
                         "confidence": r.confidence, "validated": bool(r.validated), "origin": r.origin,
                         "evidence": _evidence(r.evidence)})
    rel_rows.sort(key=lambda r: (not r["validated"], r["from"], r["to"], r["id"]))
    cand_rows = []
    for c in session.scalars(select(SemanticRelationshipCandidate).where(
            SemanticRelationshipCandidate.workspace_id == workspace_id,
            SemanticRelationshipCandidate.status.in_(("pending", "accepted")))
            .order_by(SemanticRelationshipCandidate.from_asset, SemanticRelationshipCandidate.to_asset,
                      SemanticRelationshipCandidate.id)):
        if source_id and c.source_id != source_id and not (_in_tables(c.from_asset, tables) or _in_tables(c.to_asset, tables)):
            continue
        cand_rows.append({"id": c.id, "status": c.status, "from": c.from_asset, "from_columns": list(c.from_columns or []),
                          "to": c.to_asset, "to_columns": list(c.to_columns or []), "cardinality": c.cardinality,
                          "containment": c.containment, "confidence": c.confidence, "origin": c.origin,
                          "measured_at": c.measured_at, "evidence": _evidence(c.evidence), "name": c.relationship_name})

    model_rows = []
    kept_datasets: set[str] = set()
    for m in session.scalars(select(SemanticModel).where(SemanticModel.workspace_id == workspace_id,
                                                         SemanticModel.status.in_(("approved", "proposed")))
                             .order_by(SemanticModel.version)):
        datasets = [d for d in m.datasets or [] if isinstance(d, dict)]
        rels = [r for r in m.relationships or [] if isinstance(r, dict)]
        if source_id:
            datasets = [d for d in datasets if _in_tables(str(d.get("source") or ""), tables)]
            names_kept = {str(d.get("name")) for d in datasets}
            rels = [r for r in rels if str(r.get("from") or r.get("from_dataset")) in names_kept or str(r.get("to")) in names_kept]
            if not datasets and not rels:
                continue
        kept_datasets |= {str(d.get("name")) for d in datasets}
        model_rows.append({"version": m.version, "name": m.name, "status": m.status, "description": m.description,
                           "origin": m.origin, "created_at": m.created_at, "content_hash": m.content_hash,
                           "ai_context": m.ai_context, "datasets": datasets, "relationships": rels})

    owner_ids = sorted({m for m in session.scalars(select(SemanticMetric.owner_id).where(SemanticMetric.workspace_id == workspace_id))
                        if m})
    owners = {u.id: u.name for u in session.scalars(select(User).where(User.id.in_(owner_ids)))} if owner_ids else {}
    metric_rows = []
    for m in session.scalars(select(SemanticMetric).where(SemanticMetric.workspace_id == workspace_id,
                                                          SemanticMetric.status == "approved")
                             .order_by(SemanticMetric.name, SemanticMetric.version)):
        d = m.definition or {}
        refs = [str(c) for c in d.get("source_columns") or []]
        if source_id and not (any(_in_tables(r, tables) for r in refs) or str(d.get("dataset") or "") in kept_datasets
                              or _in_tables(str(d.get("dataset") or ""), tables)):
            continue
        exprs = d.get("expressions") or []
        metric_rows.append({"name": m.name, "display_name": m.display_name, "version": m.version, "status": m.status,
                            "expression": m.expression, "dialect": (exprs[0] or {}).get("dialect") if exprs else None,
                            "description": d.get("description"), "dataset": d.get("dataset"), "grain": d.get("grain"),
                            "filters": list(d.get("filters") or []), "dimensions": list(d.get("dimensions") or []),
                            "source_columns": refs, "format": d.get("format"),
                            "owner": owners.get(m.owner_id) if m.owner_id else None, "approved_at": m.decided_at})

    glossary = []
    for e in session.scalars(select(ContextEntry).where(ContextEntry.workspace_id == workspace_id, ContextEntry.kind != "episode")
                             .order_by(ContextEntry.kind, ContextEntry.name, ContextEntry.id)):
        mapped = [str(c) for c in e.mapped_columns or []]
        if source_id and not any(_in_tables(c, tables) for c in mapped):
            continue
        glossary.append({"id": e.id, "kind": e.kind, "name": e.name, "body": e.body, "synonyms": list(e.synonyms or []),
                         "mapped_columns": mapped, "origin": e.origin, "trusted": bool(e.trusted)})
    # A column's glossary link may point at a domain pack's term, so the definition of every linked one comes along
    # (the pack's other terms stay in the pack; `visible_entries` already drops packs of other domains).
    linked = {str(c["glossary"].get("term_id")) for a in asset_rows for c in a["columns"] if isinstance(c.get("glossary"), dict)}
    for e in visible_entries(session, workspace_id, exclude_kinds=("episode",)):
        if not e.origin.startswith("pack:") or e.id not in linked:
            continue
        glossary.append({"id": e.id, "kind": e.kind, "name": e.name, "body": e.body, "synonyms": list(e.synonyms),
                         "mapped_columns": list(e.mapped_columns), "origin": e.origin, "trusted": bool(e.trusted)})

    documents = []
    for d, pack in session.execute(select(KnowledgeDocument, KnowledgePack).join(KnowledgePack, KnowledgePack.id == KnowledgeDocument.pack_id)
                                   .where(KnowledgeDocument.workspace_id == workspace_id)
                                   .order_by(KnowledgePack.slug, KnowledgeDocument.path)):
        ext = (d.frontmatter or {}).get("analystos")
        ext = ext if isinstance(ext, dict) else {}
        if source_id and ext.get("source_id") != source_id and not any(
                _in_tables(str(c), tables) for c in ext.get("mapped_columns") or []):
            continue
        documents.append({"pack": pack.slug, "pack_kind": pack.kind, "path": d.path, "title": d.title, "type": d.type,
                          "kind": d.kind, "status": d.status, "trust_tier": d.trust_tier, "revision": d.revision})

    contexts = []
    for d in session.scalars(select(Definition).where(Definition.workspace_id == workspace_id, Definition.kind == "analysis_context")
                             .order_by(Definition.key, Definition.version)):
        spec = d.spec or {}
        if source_id and source_id not in (spec.get("source_ids") or []):
            continue
        contexts.append({"key": d.key, "version": d.version, "status": d.status, "title": d.title,
                         "purpose": spec.get("purpose"), "business_description": spec.get("business_description"),
                         "question_template": spec.get("question_template"),
                         "sources": [names.get(s, s) for s in spec.get("source_ids") or []],
                         "metric_names": list(spec.get("metric_names") or []), "published_at": d.published_at})

    source_rows = []
    for s in sources:
        run = last_crawl.get(s.id)
        source_rows.append({"id": s.id, "name": s.name, "kind": s.kind, "execution_mode": s.execution_mode, "status": s.status,
                            "last_discovered_at": s.last_discovered_at, "has_error": bool(s.last_error),
                            "assets": sum(1 for a in assets if a.source_id == s.id),
                            "selected_assets": sum(1 for a in assets if a.source_id == s.id and a.selected),
                            "last_crawl": None if run is None else {
                                "id": run.id, "mode": run.mode, "trigger": run.trigger, "status": run.status,
                                "started_at": run.started_at, "finished_at": run.finished_at, "drift": run.changes or {}}})

    brief = _brief(brief_row, assertions, tables if source_id else None)
    suggested = _suggested(session, workspace_id, suggest_mod, asset_ids if source_id else None)
    content = {
        "workspace": {"id": ws.id, "name": ws.name, "slug": slug(ws.name), "objective": ws.objective or "",
                      "description": ws.description or ""},
        "scope": {"kind": "source", "source_id": source_id, "source_name": names.get(source_id)} if source_id
        else {"kind": "workspace"},
        "policy": {"data_samples_included": samples, "never_included": list(NEVER_INCLUDED)},
        "brief": brief, "sources": source_rows, "assets": asset_rows,
        "relationships": {"legacy": rel_rows, "candidates": cand_rows}, "semantic_models": model_rows,
        "metrics": metric_rows, "glossary": glossary, "knowledge_documents": documents, "analysis_contexts": contexts,
        "suggested_model": suggested,
    }
    content["counts"] = {"sources": len(source_rows), "assets": len(asset_rows),
                         "columns": sum(len(a["columns"]) for a in asset_rows),
                         "relationships": len(rel_rows), "relationship_candidates": len(cand_rows),
                         "semantic_model_versions": len(model_rows), "metrics": len(metric_rows),
                         "glossary": len(glossary), "knowledge_documents": len(documents),
                         "analysis_contexts": len(contexts), "brief_facts": len(brief["facts"]),
                         "brief_open_questions": len(brief["open_questions"])}
    return _clean(content)


def _column(c: Any, *, samples_allowed: bool) -> dict[str, Any]:
    from analystos.skills.profiling import column_is_sensitive

    sem = c.semantics or {}
    return {"name": c.name, "data_type": c.data_type, "semantic_type": c.semantic_type, "role": sem.get("semantic_role"),
            "unit": sem.get("unit"), "is_key": bool(c.is_key), "nullable": bool(c.nullable),
            "business_name": c.business_name, "business_name_origin": c.business_name_origin,
            "description": c.description, "description_origin": c.description_origin, "reviewed": bool(sem.get("reviewed")),
            "tags": sorted(c.tags or []), "pii": sem.get("pii"), "glossary": sem.get("glossary"),
            "sensitive": column_is_sensitive(c.tags, sem),
            "profile": export_profile(c.profile, c.tags, sem, samples_allowed=samples_allowed)}


def _evidence(ev: Any) -> dict[str, Any]:
    return {k: v for k, v in (ev or {}).items() if k != "sql"} if isinstance(ev, dict) else {}


def _brief(row: Any, assertions: list[Any], tables: set[str] | None) -> dict[str, Any]:
    def view(a: Any) -> dict[str, Any]:
        return {"key": a.key, "group": a.group, "field": a.field, "subject": a.subject, "value": a.value, "origin": a.origin,
                "review_state": a.review_state, "confidence": a.confidence, "note": a.note}

    kept = [a for a in assertions if tables is None or _in_tables(a.subject, tables)]
    kept.sort(key=lambda a: a.key)
    return {"version": row.version if row is not None else 0,
            "facts": [view(a) for a in kept if a.effective],
            "open_questions": [view(a) for a in kept if a.review_state == "suggested"],
            "rejected": sum(1 for a in kept if a.review_state == "rejected")}


def _suggested(session: Session, workspace_id: str, suggest_mod: Any, asset_ids: set[str] | None) -> dict[str, Any]:
    """The deterministic data-model suggestion (semantic/suggest.py) over the selected tables, with its issues."""
    try:
        s = suggest_mod.suggest(session, workspace_id)
    except Exception as exc:  # the suggestion is one section; its failure is reported, not fatal
        log.warning("context export: suggested model unavailable for %s: %s", workspace_id, exc)
        return {"available": False, "reason": exc.message if isinstance(exc, AnalystOSError) else type(exc).__name__}
    tables = [t for t in s["tables"] if asset_ids is None or t["asset_id"] in asset_ids]
    ids = {t["asset_id"] for t in tables}
    fqs = {t["fq"] for t in tables}
    rels = [r for r in s["relationships"] if r["from"]["asset_id"] in ids or r["to"]["asset_id"] in ids]
    return {"available": True, "tables": tables, "relationships": rels,
            "metrics": [m for m in s["metrics"] if m.get("table_fq") in fqs],
            "star_schemas": [x for x in s["star_schemas"] if x["fact"] in ids],
            "issues": [i for i in s["issues"] if i.get("asset_id") in ids],
            "summary": {"tables": len(tables), "relationships": len(rels),
                        "issues": sum(1 for i in s["issues"] if i.get("asset_id") in ids)}}


# ------------------------------------------------------------------------------------ json
def digest(content: Mapping[str, Any]) -> str:
    return stable_hash(dict(content))


def render_json(content: Mapping[str, Any], *, generated_at: str) -> bytes:
    """One versioned document; `content_digest` is over everything but itself and `generated_at`, so two exports
    of unchanged context carry the same digest."""
    doc = {"format": FORMAT, "generated_at": generated_at, "content_digest": digest(content), **content}
    return (json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")


# ------------------------------------------------------------------------------------ markdown
def _cell(value: Any, limit: int = 300) -> str:
    from analystos.knowledge.crawl_docs import cell

    if isinstance(value, list | tuple):
        value = ", ".join(str(v) for v in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return cell(value if value not in (None, "") else "—", limit)


def _table(headers: list[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    body = ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers), *body] if body else ["_None yet._"]


def _dataset_source(source: Any) -> str:
    """A dataset's table, or its defining query cut to one readable line (a dataset may be a SELECT, not a table)."""
    text = " ".join(str(source or "").split())
    return text if len(text) <= 80 else text[:77] + "..."


def _glossary_link(value: Any) -> Any:
    return value.get("term") or value.get("term_id") if isinstance(value, dict) else value


def profile_line(p: Mapping[str, Any] | None, *, withheld: bool = False) -> str:
    """A column profile in one line: completeness, cardinality, range, values (when the export carries them)."""
    if not p:
        return "not profiled"
    parts = []
    if p.get("null_rate") is not None:
        parts.append(f"nulls {float(p['null_rate']):.1%}")
    if p.get("distinct") is not None:
        parts.append(f"distinct {p['distinct']}")
    if p.get("min") is not None and p.get("max") is not None:
        parts.append(f"range {p['min']} .. {p['max']}")
    if p.get("mean") is not None:
        parts.append(f"mean {float(p['mean']):.4g}")
    values = p.get("values") if p.get("values_complete") else [t.get("value") for t in p.get("top_values") or []
                                                                  if isinstance(t, dict)]
    if values:
        parts.append("values " + ", ".join(str(v) for v in values[:12]))
    return "; ".join(parts + (["values withheld (sensitive)"] if withheld else [])) or "profiled"


def _scope_text(content: Mapping[str, Any]) -> str:
    scope = content.get("scope") or {}
    return f"source {scope.get('source_name') or scope.get('source_id')}" if scope.get("kind") == "source" else "whole workspace"


def _drift_text(drift: Mapping[str, Any] | None) -> str:
    if not drift:
        return "no changes recorded"
    parts = [f"{k.replace('_', ' ')} {len(v) if isinstance(v, list) else v}" for k, v in sorted(drift.items())
             if v not in (None, [], {}, 0)]
    return ", ".join(parts) or "no changes"


def render_markdown(content: Mapping[str, Any], *, generated_at: str) -> str:
    ws = content["workspace"]
    brief = content["brief"]
    out = [f"# {ws['name']} — data context", "",
           f"Exported {generated_at} · scope: {_scope_text(content)} · format {FORMAT} · content digest `{digest(content)}`", "",
           "Never included: " + ", ".join(NEVER_INCLUDED) + ". Sensitive columns show completeness and cardinality only"
           + ("; value lists are included because the workspace lets data samples reach models." if
              content["policy"]["data_samples_included"] else "; value lists are left out (the workspace policy keeps data "
                                                               "samples inside the platform)."), "",
           "## Workspace", "", f"**Objective:** {ws['objective'] or '—'}", "", f"**Description:** {ws['description'] or '—'}", "",
           "## Counts", "", *_table(["what", "count"], sorted(content["counts"].items())), "",
           f"## Brief (version {brief['version']})", "", "### Facts (reviewed or validated)", "",
           *_table(["assertion", "value", "origin", "state"], [(a["key"], a["value"], a["origin"], a["review_state"])
                                                             for a in brief["facts"]]), "",
           "### Open questions (suggested, waiting for a person)", "",
           *_table(["assertion", "suggested value", "origin", "confidence"],
                   [(a["key"], a["value"], a["origin"], a["confidence"]) for a in brief["open_questions"]]), "",
           "## Sources", "",
           *_table(["source", "kind", "execution", "status", "tables (selected)", "last crawl", "drift"],
                   [(s["name"], s["kind"], s["execution_mode"], s["status"], f"{s['assets']} ({s['selected_assets']})",
                     f"{s['last_crawl']['status']} {s['last_crawl']['finished_at'] or s['last_crawl']['started_at']}"
                     if s["last_crawl"] else "never", _drift_text((s["last_crawl"] or {}).get("drift")))
                    for s in content["sources"]]), "",
           "## Tables", ""]
    for a in content["assets"]:
        key = a.get("key") or {}
        out += [f"### {a['fq']}" + (f" — {a['business_name']}" if a.get("business_name") else ""), "",
                f"{a.get('description') or 'No description yet.'} *(description: {a.get('description_origin') or '—'}"
                f"{', reviewed' if a.get('reviewed') else ''})*", "",
                f"- source {a.get('source') or a['source_id']}; {'selected' if a['selected'] else 'not selected'}; "
                f"role {a.get('role') or '—'}, domain {a.get('domain') or '—'}, entity {a.get('entity') or '—'}",
                f"- grain {a.get('grain') or '—'}; rows {a.get('row_count') if a.get('row_count') is not None else '—'}; "
                f"key {', '.join(key.get('columns') or []) or '—'} ({key.get('evidence') or 'none'}"
                f"{', unique' if key.get('unique') is True else ', not unique' if key.get('unique') is False else ''}); "
                f"time column {a.get('time_column') or '—'}", "",
                *_table(["column", "type", "role", "unit", "business name", "description", "tags / PII", "glossary", "profile"],
                        [(c["name"], c["data_type"], c.get("role"), c.get("unit"),
                          c.get("business_name"), (c.get("description") or "") + (f" ({c['description_origin']})"
                                                                                  if c.get("description_origin") else ""),
                          ", ".join(c.get("tags") or []) + (f" PII {(c.get('pii') or {}).get('category')}"
                                                            if isinstance(c.get("pii"), dict) and c["pii"].get("category") else ""),
                          _glossary_link(c.get("glossary")), profile_line(c.get("profile"), withheld=bool(c.get("sensitive")) and not
                                                                 content["policy"]["data_samples_included"]))
                         for c in a["columns"]]), ""]
    rels = content["relationships"]
    out += ["## Relationships", "", "### Known joins", "",
            *_table(["from", "to", "cardinality", "validated", "origin", "confidence"],
                    [(r["from"], r["to"], r["cardinality"], "yes" if r["validated"] else "no", r["origin"], r["confidence"])
                     for r in rels["legacy"]]), "",
            "### Measured candidates (review queue)", "",
            *_table(["from", "to", "measured cardinality", "containment", "status", "evidence"],
                    [(f"{r['from']}({', '.join(r['from_columns'])})", f"{r['to']}({', '.join(r['to_columns'])})", r["cardinality"],
                      r["containment"], r["status"], r["evidence"]) for r in rels["candidates"]]), "",
            "## Semantic model", ""]
    for m in content["semantic_models"]:
        out += [f"### {m['name']} v{m['version']} ({m['status']})", "", m.get("description") or "", "",
                *_table(["dataset", "table", "primary key", "fields"],
                        [(d.get("name"), _dataset_source(d.get("source")), d.get("primary_key"), len(d.get("fields") or []))
                         for d in m["datasets"]]), "",
                *_table(["relationship", "from", "to", "columns", "cardinality"],
                        [(r.get("name"), r.get("from") or r.get("from_dataset"), r.get("to"),
                          f"{r.get('from_columns')} -> {r.get('to_columns')}", r.get("cardinality")) for r in m["relationships"]]), ""]
    if not content["semantic_models"]:
        out += ["No approved or proposed semantic model yet.", ""]
    out += ["## Approved metrics", "",
            *_table(["metric", "formula", "version", "owner", "description"],
                    [(m["display_name"] or m["name"], m["expression"], m["version"], m["owner"], m["description"])
                     for m in content["metrics"]]), "",
            "## Glossary, definitions and rules", "",
            *_table(["kind", "name", "definition", "synonyms", "columns", "origin"],
                    [(g["kind"], g["name"], g["body"], g["synonyms"], g["mapped_columns"],
                      g["origin"] + ("" if g["trusted"] else " (unreviewed)")) for g in content["glossary"]]), "",
            "## Knowledge documents", "",
            *_table(["pack", "path", "title", "type", "status"],
                    [(d["pack"], d["path"], d["title"], d["type"], d["status"]) for d in content["knowledge_documents"]]), "",
            "## Analysis contexts", "",
            *_table(["context", "version", "status", "purpose", "sources", "metrics"],
                    [(c["title"] or c["key"], c["version"], c["status"], c["purpose"], c["sources"], c["metric_names"])
                     for c in content["analysis_contexts"]]), ""]
    out += _suggested_markdown(content["suggested_model"], heading="## Suggested data model")
    return "\n".join(out).rstrip() + "\n"


def _suggested_markdown(s: Mapping[str, Any], *, heading: str) -> list[str]:
    out = [heading, ""]
    if not s.get("available"):
        return out + [f"Not available: {s.get('reason')}", ""]
    out += ["Measured from the catalog, no query and no model. A suggestion only: nothing here is approved.", "",
            *_table(["table", "role", "entity", "key (evidence)", "time column", "measures", "dimensions", "issues"],
                    [(t["fq"], t["role"], t.get("entity"), f"{', '.join(t['primary_key']['columns']) or '—'} "
                      f"({t['primary_key']['evidence']})", t.get("time_column"), t["measures"], t["dimensions"],
                      "; ".join(i["message"] for i in t["issues"])) for t in s["tables"]]), "",
            *_table(["relationship", "cardinality", "status"],
                    [(f"{r['from']['fq']}({', '.join(r['from']['columns'])}) -> {r['to']['fq']}({', '.join(r['to']['columns'])})",
                      r.get("cardinality"), r.get("status")) for r in s["relationships"]]), "",
            *_table(["candidate metric", "expression", "table", "why"],
                    [(m["label"], m["expression"], m["table_fq"], m["reason"]) for m in s["metrics"]]), "",
            *_table(["issue", "message"], [(i["code"], i["message"]) for i in s["issues"]]), ""]
    return out


# ------------------------------------------------------------------------------------ OKF
def _bounded(text: str) -> str:
    """A document under the OKF per-document cap: whole lines, then a note that the JSON export has the rest."""
    data = text.encode("utf-8")
    if len(data) <= _DOC_LIMIT:
        return text
    cut = data[:_DOC_LIMIT].decode("utf-8", errors="ignore")
    cut = cut[: cut.rfind("\n")]
    rest = text[len(cut):].count("\n")
    return f"{cut}\n\n… {rest} more lines: see the JSON export for the complete content.\n"


def _doc(frontmatter: dict[str, Any], body: str) -> str:
    from analystos.knowledge import okf

    return _bounded(okf.render_document(_clean(frontmatter), body))


def render_okf(content: Mapping[str, Any], *, generated_at: str) -> dict[str, bytes]:
    """{path: bytes} of an OKF v0.2 bundle rooted at the archive root. Deterministic: `generated_at` only names
    the export in index.md's text."""
    from analystos.knowledge import crawl_docs
    from analystos.knowledge.entries import render_entry

    ws = content["workspace"]
    files: dict[str, str] = {}
    by_source: dict[str, list[str]] = defaultdict(list)
    for a in content["assets"]:
        cols = [{"name": c["name"], "data_type": c["data_type"], "business_name": c.get("business_name"),
                 "description": c.get("description"), "tags": c.get("tags")} for c in a["columns"]]
        asset = {"id": a["id"], "schema_name": a["schema_name"], "name": a["name"], "kind": a["kind"], "lifecycle": "active",
                 "business_name": a.get("business_name"), "description": a.get("description"), "reviewed": a.get("reviewed"),
                 "description_origin": a.get("description_origin"), "fingerprint": None,
                 "semantics": {k: a[k] for k in ("role", "domain", "grain") if a.get(k)}}
        docs = crawl_docs.table_documents(asset, cols, source_id=a["source_id"], source_name=a.get("source") or a["source_id"])
        if crawl_docs.table_path(a["fq"]) in files:  # two sources expose the same schema.table: the first one wins
            continue
        files.update({p: _bounded(t) for p, t in docs.items()})
        by_source[a["source_id"]].append(a["fq"])
    for s in content["sources"]:
        files[crawl_docs.source_path(s["id"])] = _bounded(crawl_docs.source_document(
            {"id": s["id"], "name": s["name"], "kind": s["kind"], "execution_mode": s["execution_mode"]}, by_source.get(s["id"], [])))

    brief = content["brief"]
    files["brief.md"] = _doc(
        {"type": "Workspace Brief", "title": f"{ws['name']} brief", "status": "stable", "tags": ["brief"],
         "generated": {"by": GENERATOR}, "analystos": {"kind": "brief", "origin": "platform", "trusted": False,
                                                       "brief_version": brief["version"]}},
        "\n".join(["# Workspace", "", f"Objective: {ws['objective'] or '—'}", "", f"Description: {ws['description'] or '—'}", "",
                   "# Facts", "", *_table(["assertion", "value", "origin", "state"],
                                          [(a["key"], a["value"], a["origin"], a["review_state"]) for a in brief["facts"]]), "",
                   "# Open questions", "", *_table(["assertion", "suggested value", "origin", "confidence"],
                                                   [(a["key"], a["value"], a["origin"], a["confidence"])
                                                    for a in brief["open_questions"]])]))

    used: set[str] = set()

    def unique(base: str) -> str:
        path, n = base, 2
        while path in used:
            path, n = base.replace(".md", f"-{n}.md"), n + 1
        used.add(path)
        return path

    for g in content["glossary"]:
        if str(g["origin"]).startswith("pack:"):
            continue  # a domain pack's own terms re-import from the pack, not as copies in this workspace
        path = unique(f"glossary/{crawl_docs.safe_segment(g['kind'])}-{slug(g['name'])}.md")
        files[path] = _bounded(render_entry(kind=g["kind"], name=g["name"], body=g["body"] or g["name"], synonyms=g["synonyms"],
                                            mapped_columns=g["mapped_columns"], origin=g["origin"], trusted=g["trusted"],
                                            generated_by=GENERATOR))
    for m in content["metrics"]:
        body = "\n\n".join(x for x in (m.get("description"), f"Formula ({m.get('dialect') or 'sql'}): `{m['expression']}`",
                                       f"Approved version {m['version']}" + (f", owner {m['owner']}." if m.get("owner") else "."),
                                       f"Dataset: {m['dataset']}." if m.get("dataset") else None) if x)
        path = unique(f"metrics/{slug(m['name'])}-v{m['version']}.md")
        files[path] = _bounded(render_entry(kind="metric", name=m.get("display_name") or m["name"], body=body,
                                            synonyms=[m["name"]] if m.get("display_name") and m["display_name"] != m["name"] else (),
                                            mapped_columns=m["source_columns"], origin="semantic_model", trusted=True,
                                            generated_by=GENERATOR))
    for m in content["semantic_models"]:
        files[f"model/semantic-model-v{m['version']}.md"] = _doc(
            {"type": "Semantic Model", "title": f"{m['name']} v{m['version']}", "status": "stable" if m["status"] == "approved"
             else "draft", "tags": ["semantic-model"], "generated": {"by": GENERATOR},
             "analystos": {"kind": "semantic_model", "origin": m["origin"], "trusted": m["status"] == "approved",
                           "version": m["version"], "content_hash": m["content_hash"]}},
            "\n".join(["# Datasets", "", *_table(["dataset", "table", "primary key", "fields"],
                                                 [(d.get("name"), _dataset_source(d.get("source")), d.get("primary_key"), len(d.get("fields") or []))
                                                  for d in m["datasets"]]), "", "# Relationships", "",
                       *_table(["relationship", "from", "to", "columns", "cardinality"],
                               [(r.get("name"), r.get("from") or r.get("from_dataset"), r.get("to"),
                                 f"{r.get('from_columns')} -> {r.get('to_columns')}", r.get("cardinality"))
                                for r in m["relationships"]])]))
    rels = content["relationships"]
    files["model/relationships.md"] = _doc(
        {"type": "Relationships", "title": f"{ws['name']} joins", "status": "draft", "tags": ["relationships"],
         "generated": {"by": GENERATOR}, "analystos": {"kind": "relationships", "origin": "platform", "trusted": False}},
        "\n".join(["# Known joins", "", *_table(["from", "to", "cardinality", "validated", "origin"],
                                                [(r["from"], r["to"], r["cardinality"], "yes" if r["validated"] else "no", r["origin"])
                                                 for r in rels["legacy"]]), "", "# Measured candidates", "",
                   *_table(["from", "to", "measured cardinality", "containment", "status"],
                           [(f"{r['from']}({', '.join(r['from_columns'])})", f"{r['to']}({', '.join(r['to_columns'])})",
                             r["cardinality"], r["containment"], r["status"]) for r in rels["candidates"]])]))
    files["model/suggested.md"] = _doc(
        {"type": "Suggested Data Model", "title": f"{ws['name']} suggested model", "status": "draft",
         "tags": ["suggested-model"], "generated": {"by": GENERATOR},
         "analystos": {"kind": "suggested_model", "origin": "rule", "trusted": False}},
        "\n".join(_suggested_markdown(content["suggested_model"], heading="# Suggested data model")))
    for c in content["analysis_contexts"]:
        path = unique(f"contexts/{slug(c['key'])}-v{c['version']}.md")
        files[path] = _doc(
            {"type": "Analysis Context", "title": c["title"] or c["key"], "status": "stable" if c["status"] == "published"
             else "draft", "tags": ["analysis-context"], "generated": {"by": GENERATOR},
             "analystos": {"kind": "analysis_context", "origin": "user", "trusted": c["status"] == "published",
                           "version": c["version"]}},
            "\n".join(["# Purpose", "", c.get("purpose") or "—", "", "# Business description", "",
                       c.get("business_description") or "—", "", "# Question template", "", c.get("question_template") or "—",
                       "", f"Sources: {', '.join(c['sources']) or '—'}. Metrics: {', '.join(c['metric_names']) or '—'}."]))

    files["index.md"] = _index(content, files, generated_at)
    return {p: t.encode("utf-8") for p, t in sorted(files.items())}


def _index(content: Mapping[str, Any], files: Mapping[str, str], generated_at: str) -> str:
    from analystos.knowledge import okf

    ws = content["workspace"]
    groups: dict[str, list[str]] = defaultdict(list)
    for p in sorted(files):
        groups[p.split("/", 1)[0] if "/" in p else ""].append(p)
    body = [f"# {ws['name']} — data context", "",
            f"Exported {generated_at} by AnalystOS ({_scope_text(content)}); content digest `{digest(content)}`. "
            f"Never included: {', '.join(NEVER_INCLUDED)}.", "", "- [Brief](brief.md)"]
    titles = {"sources": "Sources", "tables": "Tables", "glossary": "Glossary, definitions and rules", "metrics": "Metrics",
              "model": "Data model", "contexts": "Analysis contexts"}
    for group, title in titles.items():
        if groups.get(group):
            body += ["", f"# {title}", ""] + [f"- [{p.split('/', 1)[1][:-3]}]({p})" for p in groups[group]]
    docs = content["knowledge_documents"]
    if docs:
        body += ["", "# Knowledge documents (in the workspace packs, not copied here)", ""]
        body += [f"- {d['pack']}/{d['path']}: {d['title']} ({d['type']}, {d['status']})" for d in docs]
    return _bounded(f"---\nokf_version: '{okf.OKF_VERSION}'\n---\n\n" + "\n".join(body).strip() + "\n")


# ------------------------------------------------------------------------------------ export
def filename(content: Mapping[str, Any], fmt: str, day: str) -> str:
    scope = content.get("scope") or {}
    part = f"-{slug(scope.get('source_name') or scope.get('source_id') or 'source', 40)}" if scope.get("kind") == "source" else ""
    return f"{content['workspace']['slug']}{part}-context-{day}.{FORMATS[fmt][1]}"


def render(content: Mapping[str, Any], fmt: str, *, generated_at: str) -> bytes:
    if fmt == "json":
        return render_json(content, generated_at=generated_at)
    if fmt == "markdown":
        return render_markdown(content, generated_at=generated_at).encode("utf-8")
    if fmt == "okf":
        from analystos.knowledge.bundle import zip_bytes

        return zip_bytes(render_okf(content, generated_at=generated_at))
    raise InvalidInput(f"format must be one of {', '.join(FORMATS)}")


__all__ = ["FORMAT", "FORMATS", "NEVER_INCLUDED", "collect", "digest", "export_profile", "filename", "profile_line", "render",
           "render_json", "render_markdown", "render_okf"]
