"""Crawler sources beyond the database catalog (P4-K06, spec v3 §6.3): value-free query history, dbt
manifests, Superset metadata and uploaded documents. Each writes OKF drafts into the workspace pack
through `knowledge/drafts.write_drafts` (curated documents kept, tags only tighten), records its
facets (`services/facets.py`), and is audited.

None of these touches source data: query history reads the platform's own query audit, the others
read artifacts the caller supplies (a manifest, a document) or the BI tool's metadata API (GET only).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput
from analystos.db.models import QueryExecution, Source, SourceAsset, SourceColumn, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.knowledge.drafts import write_drafts
from analystos.services.facets import Facets

log = logging.getLogger(__name__)

HISTORY_LIMIT = 5000  # most recent audited statements per source
MAX_MANIFEST_BYTES = 64 * 1024 * 1024


def _finish(session: Session, workspace_id: str, actor: str, kind: str, facets: Facets, report: Any,
            extra: dict[str, Any]) -> dict[str, Any]:
    out = {"kind": kind, "facets": facets.as_dict(), "failed_facets": facets.failed, **report.as_dict(), **extra}
    audit(actor, f"knowledge.ingested.{kind}", workspace_id=workspace_id, target=kind,
          decision="allow" if not facets.failed else "partial",
          details={k: v for k, v in out.items() if k not in ("written", "unchanged")} | {"written": len(report.written)},
          session=session)
    emit(workspace_id, "knowledge.ingested", {"kind": kind, "written": len(report.written),
                                              "kept_curated": len(report.kept_curated), "failed_facets": facets.failed,
                                              "revision": report.revision}, actor=actor, session=session)
    return out


def _savepoint(session: Session, fn: Any, *args: Any) -> Any:
    """Run a facet's writes in a savepoint, so a facet that fails half-way leaves nothing behind."""
    with session.begin_nested():
        return fn(*args)


# ------------------------------------------------------------------------------------ query history
def mine_query_history(session: Session, workspace_id: str, *, source_ids: list[str] | None = None, author: str,
                       limit: int = HISTORY_LIMIT) -> dict[str, Any]:
    """Structure-only patterns of the workspace's successful governed queries, one document per source.
    Statements are parsed in memory; no literal from them is stored anywhere."""
    from analystos.connectors.kinds import dialect_for
    from analystos.knowledge import crawl_docs
    from analystos.skills.query_history import mine

    sources = list(session.scalars(select(Source).where(Source.workspace_id == workspace_id,
                                                        *([Source.id.in_(source_ids)] if source_ids else []))
                                   .order_by(Source.id)))
    docs: dict[str, str] = {}
    total = 0
    for src in sources:
        rows = session.execute(select(QueryExecution.sql, QueryExecution.created_at).where(
            QueryExecution.workspace_id == workspace_id, QueryExecution.source_id == src.id, QueryExecution.status == "ok",
            QueryExecution.purpose.notlike("crawl.%")).order_by(QueryExecution.created_at.desc()).limit(limit)).all()
        if not rows:
            continue
        try:
            dialect = dialect_for(src.kind, src.execution_mode)
        except Exception:  # noqa: BLE001 - an unknown kind parses as the staged dialect
            dialect = "postgres"
        patterns = mine((r.sql for r in rows), dialect=dialect)
        total += patterns.statements
        known = {f"{a.schema_name}.{a.name}".lower(): f"{a.schema_name}.{a.name}"
                 for a in session.scalars(select(SourceAsset).where(SourceAsset.source_id == src.id))}
        window = (min(r.created_at for r in rows).isoformat(), max(r.created_at for r in rows).isoformat())
        docs[crawl_docs.query_patterns_path(src.id)] = crawl_docs.query_patterns_document(
            {"id": src.id, "name": src.name}, patterns.as_dict(), window=window, known_tables=set(known))
    report = write_drafts(session, workspace_id, docs, author=author, reason="query history (structure only)",
                          origin="crawler", meta={"query_history": {"sources": [s.id for s in sources], "statements": total}})
    return {"statements": total, "sources": len(docs), **report.as_dict()}


def query_history(session: Session, user: User, workspace_id: str, *, source_ids: list[str] | None = None) -> dict[str, Any]:
    require_role(session, user, workspace_id, "editor")
    facets = Facets()
    result = facets.run("query_history", mine_query_history, session, workspace_id, source_ids=source_ids,
                        author=f"human:{user.id}")
    from analystos.knowledge.drafts import DraftWrite

    report = DraftWrite(**{k: result[k] for k in ("written", "unchanged", "kept_curated", "revision")}) if result else DraftWrite()
    return _finish(session, workspace_id, f"user:{user.id}", "query_history", facets, report,
                   {"statements": (result or {}).get("statements", 0)})


# ------------------------------------------------------------------------------------ dbt manifest
def _load_manifest(manifest: dict[str, Any] | bytes | str) -> dict[str, Any]:
    if isinstance(manifest, dict):
        return manifest
    raw = manifest.encode() if isinstance(manifest, str) else manifest
    if len(raw) > MAX_MANIFEST_BYTES:
        raise InvalidInput(f"manifest is {len(raw)} bytes; the limit is {MAX_MANIFEST_BYTES}")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise InvalidInput("manifest.json is not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidInput("manifest.json must be a JSON object")
    return data


def _apply_dbt_to_catalog(session: Session, workspace_id: str, nodes: list[Any]) -> dict[str, int]:
    """dbt descriptions fill the catalog where the crawler precedence allows (never over user, model or
    reviewed text; an existing source description is kept); dbt `pii` tags only add tags."""
    from analystos.services.crawler import _description_writable
    from analystos.skills import catalog as cat

    assets = list(session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                            SourceAsset.lifecycle == "active")))
    by_fq = {f"{a.schema_name}.{a.name}".lower(): a for a in assets}
    by_name: dict[str, list[SourceAsset]] = {}
    for a in assets:
        for n in {a.name.lower(), (a.source_name or "").lower()} - {""}:
            by_name.setdefault(n, []).append(a)
    matched = described = columns = tagged = 0
    for node in nodes:
        a = by_fq.get(node.fq.lower())
        if a is None:
            candidates = by_name.get(node.relation.lower(), [])
            a = candidates[0] if len(candidates) == 1 else None
        if a is None:
            continue
        matched += 1
        desc = cat.screen_text(node.description, max_chars=1000) if node.description else ""
        if desc and not cat.is_placeholder_description(desc, table_name=a.name) \
                and _description_writable(a.description_origin, a.reviewed, a.description, a.name):
            a.description, a.description_origin = desc, "source"
            described += 1
        cols = {c.name.lower(): c for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id))}
        for dc in node.columns:
            col = cols.get(dc.name.lower())
            if col is None:
                continue
            text = cat.screen_text(dc.description, max_chars=500) if dc.description else ""
            if text and col.tags_origin != "user" and (not col.description or cat.is_placeholder_description(col.description)):
                col.description = text
                columns += 1
            if dc.pii and "pii" not in (col.tags or []):
                col.tags = sorted({*(col.tags or []), "pii"})  # tags only tighten
                tagged += 1
    return {"matched": matched, "descriptions": described, "column_descriptions": columns, "pii_tags": tagged}


def ingest_dbt_manifest(session: Session, user: User, workspace_id: str, manifest: dict[str, Any] | bytes | str) -> dict[str, Any]:
    """A dbt manifest (v12+): model/source documents with descriptions, tests and lineage into the
    pack; descriptions and pii tags into the catalog under the crawler precedence; lineage edges."""
    from analystos.artifacts.registry import link
    from analystos.knowledge import dbt_manifest as dbt

    require_role(session, user, workspace_id, "editor")
    data = _load_manifest(manifest)
    nodes = dbt.parse_manifest(data)  # InvalidInput for anything that is not a v12+ manifest: fails the call
    facets = Facets()
    docs = facets.run("dbt.documents", dbt.render_documents, data, nodes) or {}
    catalog = facets.run("dbt.catalog", _savepoint, session, _apply_dbt_to_catalog, session, workspace_id, nodes) or {}

    def lineage() -> dict[str, Any]:
        edges = dbt.lineage_edges(nodes)
        for up, down in edges:
            link(session, workspace_id, ("table", up), "transformed_into", ("table", down))
        return {"count": len(edges)}

    edges = facets.run("dbt.lineage", _savepoint, session, lineage) or {}
    report = write_drafts(session, workspace_id, docs, author=dbt.DBT_ACTOR, reason=f"dbt manifest of {dbt.project_name(data)}",
                          origin="dbt", meta={"dbt": {"project": dbt.project_name(data), "nodes": len(nodes),
                                                      "schema_version": dbt.schema_version(data)}})
    return _finish(session, workspace_id, f"user:{user.id}", "dbt_manifest", facets, report,
                   {"project": dbt.project_name(data), "nodes": len(nodes), "catalog": catalog,
                    "lineage_edges": edges.get("count", 0)})


# ------------------------------------------------------------------------------------ Superset
def superset_metadata(session: Session, user: User, workspace_id: str, *, include: list[str] | None = None,
                      client: Any = None) -> dict[str, Any]:
    """Datasets, charts and dashboards the workspace may see in Superset, read-only, as documents."""
    from analystos.core.config import get_settings
    from analystos.knowledge import superset_meta
    from analystos.services.platform_settings import get as platform

    require_role(session, user, workspace_id, "editor")
    settings = get_settings()
    facets = Facets()
    own_client = client is None
    if client is None:
        if not platform().features.superset_publishing:
            raise InvalidInput("Superset is turned off by the administrator")
        from analystos.publishing.superset import SupersetClient

        client = SupersetClient(settings.superset_url, settings.superset_username, settings.superset_password)
    try:
        docs = superset_meta.collect(client, workspace_id, facets, include=include,
                                     base_url=(settings.superset_public_url or settings.superset_url).rstrip("/"))
    finally:
        if own_client:
            client.close()
    report = write_drafts(session, workspace_id, docs, author=superset_meta.SUPERSET_ACTOR, reason="Superset metadata",
                          origin="superset", meta={"superset": {k: v.get("count") for k, v in facets.as_dict().items()}})
    return _finish(session, workspace_id, f"user:{user.id}", "superset", facets, report,
                   {"objects": {k.split(".", 1)[1]: v.get("count", 0) for k, v in facets.as_dict().items()}})


# ------------------------------------------------------------------------------------ documents
def upload_document(session: Session, user: User, workspace_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """Markdown, text or PDF → draft `Document` sections in the pack (screened; see knowledge/documents.py)."""
    from analystos.knowledge.documents import DOCUMENT_ACTOR, document_drafts

    require_role(session, user, workspace_id, "editor")
    docs, info = document_drafts(filename, data, uploaded_by=f"human:{user.id}")
    facets = Facets()
    facets.run("document.extract", lambda: {"count": info["parts"]})
    report = write_drafts(session, workspace_id, docs, author=DOCUMENT_ACTOR, reason=f"upload {info['filename']}",
                          origin="upload", meta={"upload": {k: info[k] for k in ("filename", "sha256", "bytes", "media_type")}})
    return _finish(session, workspace_id, f"user:{user.id}", "document", facets, report, {"document": info})
