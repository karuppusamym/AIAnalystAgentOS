"""Download and inspect the workspace context (Stream D): the whole catalog, brief, model, metrics and glossary as
OKF, JSON or Markdown (for the workspace or one source); what a model purpose's prompt would carry, without
calling a model; and the workspace's shared context-cache entries.

An export is a download to the requesting user's browser (like a knowledge-pack export), not a write outside
the platform, so it needs no approval; it is audited (`context.exported`)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.core.ids import utcnow
from analystos.db.models import Source, User
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role

router = APIRouter(prefix="/api", tags=["context"])


def _source(session: Session, user: User, workspace_id: str, source_id: str | None) -> Source | None:
    return load_in_workspace(session, Source, source_id, workspace_id, user=user, label="source") if source_id else None


@router.get("/workspaces/{workspace_id}/context/export", response_class=Response)
def export_context(workspace_id: str, format: Literal["okf", "json", "markdown"] = "okf", source_id: str | None = None,
                   user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The workspace's data context as one download: `okf` (an OKF v0.2 zip that re-imports through
    `POST /knowledge/import`), `json` (`analystos.context/v1` with a content digest) or `markdown`. With
    `source_id`, only that source's tables and what references them. Never secrets, connection settings or rows."""
    from analystos.context import export

    require_role(session, user, workspace_id, "viewer")
    _source(session, user, workspace_id, source_id)
    now = utcnow()
    content = export.collect(session, workspace_id, source_id=source_id)
    data = export.render(content, format, generated_at=now.isoformat(timespec="seconds"))
    name = export.filename(content, format, now.date().isoformat())
    audit(f"user:{user.id}", "context.exported", workspace_id=workspace_id, target=source_id or workspace_id, decision="allow",
          details={"format": format, "source_id": source_id, "bytes": len(data), "digest": export.digest(content),
                   "counts": content["counts"]}, session=session)
    return Response(data, media_type=export.FORMATS[format][0],
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/workspaces/{workspace_id}/context/purposes")
def context_purposes(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The model purposes the preview can show, with plain-language labels."""
    from analystos.context import preview

    require_role(session, user, workspace_id, "viewer")
    return preview.purposes()


@router.get("/workspaces/{workspace_id}/context/preview")
def preview_context(workspace_id: str, purpose: str = "sql_generation", question: str | None = None, source_id: str | None = None,
                    download: bool = False, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """What the agents see: the prompt context `purpose` would send for `question` (system text, cached workspace
    preamble, per-call inputs), with token estimates, section sizes and whether the shared context cache already
    holds its knowledge retrieval. No model is called and nothing is recorded as a call. `download=1` returns it
    as a text file."""
    from analystos.context import export, preview
    from analystos.governance.policy import get_workspace

    require_role(session, user, workspace_id, "viewer")
    _source(session, user, workspace_id, source_id)
    out = preview.build(session, user, workspace_id, purpose=purpose, question=question, source_id=source_id)
    if not download:
        return out
    name = f"{export.slug(get_workspace(session, workspace_id).name)}-context-preview-{purpose}-{utcnow().date().isoformat()}.txt"
    return PlainTextResponse(preview.as_text(out), headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/workspaces/{workspace_id}/context/cache")
def context_cache_stats(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """This workspace's shared context cache: live entries per kind (compiled contexts, knowledge retrievals) and
    hits, misses and characters reused per kind and purpose. Redis when configured, else this API process only."""
    from analystos.context import cache as context_cache
    from analystos.services.platform_settings import get as platform

    require_role(session, user, workspace_id, "editor")
    settings = platform().context
    return {**context_cache.workspace_stats(workspace_id), "enabled": settings.cache_enabled,
            "ttl_seconds": settings.cache_ttl_seconds}


@router.delete("/workspaces/{workspace_id}/context/cache")
def clear_context_cache(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Drop this workspace's cached contexts and retrievals (owner): the next calls compile afresh."""
    from analystos.context import cache as context_cache

    require_role(session, user, workspace_id, "owner")
    removed = context_cache.clear_workspace(workspace_id)
    audit(f"user:{user.id}", "context.cache_cleared", workspace_id=workspace_id, target=workspace_id, decision="allow",
          details={"entries": removed}, session=session)
    return {"cleared": removed}
