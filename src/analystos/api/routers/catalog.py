"""Data catalog: source kinds, metadata crawls, curated asset metadata."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row
from analystos.core.errors import InvalidInput, NotFound
from analystos.db.models import CrawlRun, Source, SourceAsset, SourceColumn, User
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.services import crawler

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/source-kinds")
def source_kinds(user: User = Depends(current_user)):
    """Every database/file/API kind the platform can connect, with the fields a form needs,
    whether its driver is installed and enabled by the administrator, and whether it is certified
    (a dated live-run evidence file exists; connectors/certification.py)."""
    from analystos.connectors import certification, kinds
    from analystos.services.platform_settings import get as platform

    enabled = set(platform().sources.enabled_kinds)
    certs = certification.statuses()
    return [{**k.model_dump(include={"kind", "label", "category", "required", "optional", "default_port", "docs"}),
             "secret_field": k.secret.field if k.secret else None,
             "execution_mode": kinds.execution_mode_for(k.kind), "dialect": kinds.dialect_for(k.kind),
             "driver_installed": kinds.driver_available(k.kind),
             "install_hint": (f"pip install 'analystos[{k.driver.extra}]'" if k.driver.extra else
                              ("pip install " + " ".join(k.driver.packages) if k.driver.packages else None)),
             "enabled": not enabled or k.kind in enabled,
             "certified": certs[k.kind]["status"] == "certified", "certification": certs[k.kind]}
            for k in kinds.list_kinds()]


class CrawlIn(BaseModel):
    mode: str | None = None  # full | incremental (default: admin crawl.default_mode)
    include: list[str] | None = None
    exclude: list[str] | None = None
    profile: bool | None = None
    enrich: bool | None = None


@router.post("/workspaces/{workspace_id}/sources/{source_id}/crawl")
def start_crawl(workspace_id: str, source_id: str, body: CrawlIn, background: BackgroundTasks,
                user: User = Depends(current_user), session: Session = Depends(db)):
    src = session.get(Source, source_id)
    if src is None or src.workspace_id != workspace_id:
        raise NotFound("source not found")
    run = crawler.start_crawl(session, user, source_id, **body.model_dump())
    view = crawler.crawl_view(run)
    session.commit()  # the background task reads the crawl_run row in its own session: it must be visible first
    background.add_task(_run_quietly, run.id, user.id)
    return view


def _run_quietly(crawl_id: str, user_id: str) -> None:
    try:
        crawler.run_crawl(crawl_id, user_id)
    except Exception:  # recorded on the crawl_run by run_crawl; logged here so nothing fails silently
        logging.getLogger(__name__).exception("background crawl %s failed", crawl_id)


@router.get("/workspaces/{workspace_id}/crawls")
def list_crawls(workspace_id: str, source_id: str | None = None, limit: int = 50, user: User = Depends(current_user),
                session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(CrawlRun).where(CrawlRun.workspace_id == workspace_id)
    if source_id:
        stmt = stmt.where(CrawlRun.source_id == source_id)
    return [crawler.crawl_view(r) for r in session.scalars(stmt.order_by(CrawlRun.started_at.desc()).limit(min(limit, 200)))]


@router.get("/crawls/{crawl_id}")
def get_crawl(crawl_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    r = session.get(CrawlRun, crawl_id)
    if r is None:
        raise NotFound("crawl not found")
    require_role(session, user, r.workspace_id, "viewer")
    return crawler.crawl_view(r)


@router.get("/workspaces/{workspace_id}/catalog")
def catalog(workspace_id: str, q: str = "", domain: str | None = None, role: str | None = None, include_deprecated: bool = False,
            user: User = Depends(current_user), session: Session = Depends(db)):
    """Searchable catalog built by the crawler (names, business names, descriptions, role/domain)."""
    require_role(session, user, workspace_id, "viewer")
    stmt = select(SourceAsset).where(SourceAsset.workspace_id == workspace_id)
    if not include_deprecated:
        stmt = stmt.where(SourceAsset.lifecycle == "active")
    needle = q.strip().lower()
    out = []
    for a in session.scalars(stmt.order_by(SourceAsset.schema_name, SourceAsset.name)):
        sem = a.semantics or {}
        if domain and sem.get("domain") != domain or role and sem.get("role") != role:
            continue
        cols = list(session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal)))
        hay = " ".join([a.name, a.business_name or "", a.description or "", *(c.name for c in cols),
                        *(c.business_name or "" for c in cols)]).lower()
        if needle and needle not in hay:
            continue
        out.append({"id": a.id, "fq": f"{a.schema_name}.{a.name}", "source_id": a.source_id, "name": a.name,
                    "business_name": a.business_name, "business_name_origin": a.business_name_origin,
                    "description": a.description, "description_origin": a.description_origin,
                    "reviewed": a.reviewed, "selected": a.selected, "lifecycle": a.lifecycle, "row_count": a.row_count,
                    "role": sem.get("role"), "domain": sem.get("domain"), "grain": sem.get("grain"),
                    "confidence": sem.get("confidence"), "last_crawled_at": a.last_crawled_at,
                    "columns": [{"name": c.name, "data_type": c.data_type, "business_name": c.business_name,
                                 "description": c.description, "tags": c.tags, "tags_origin": c.tags_origin,
                                 "role": (c.semantics or {}).get("semantic_role"), "unit": (c.semantics or {}).get("unit"),
                                 "pii": (c.semantics or {}).get("pii"), "glossary": (c.semantics or {}).get("glossary")}
                                for c in cols]})
    return out


class AssetMetadataIn(BaseModel):
    business_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    reviewed: bool | None = None


@router.patch("/assets/{asset_id}/metadata")
def curate_asset(asset_id: str, body: AssetMetadataIn, user: User = Depends(current_user), session: Session = Depends(db)):
    """A person's curation wins over every crawler/model description from now on."""
    a = session.get(SourceAsset, asset_id)
    if a is None:
        raise NotFound("asset not found")
    require_role(session, user, a.workspace_id, "editor")
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise InvalidInput("nothing to change")
    if "business_name" in patch:
        a.business_name, a.business_name_origin = patch["business_name"].strip() or None, "user"
    if "description" in patch:
        a.description, a.description_origin = patch["description"].strip() or None, "user"
    if "reviewed" in patch:
        a.reviewed = patch["reviewed"]
    audit(f"user:{user.id}", "asset.curated", workspace_id=a.workspace_id, target=f"{a.schema_name}.{a.name}",
          details=patch, session=session)
    return row(a)
