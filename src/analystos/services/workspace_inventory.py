"""Read-only inventory for a workspace before retention or erasure decisions."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Table, func, select
from sqlalchemy.orm import Session

from analystos.connectors.naming import staging_schema_for
from analystos.core.config import get_settings
from analystos.db.base import Base
from analystos.db.models import BuildJob, BuildTarget, Publication, Source, User
from analystos.governance.policy import get_workspace, require_role


def _workspace_path(table: Table, seen: frozenset[str] = frozenset()) -> tuple[tuple[Any, Any], ...] | None:
    """Foreign-key route from a child row to its owning workspace, if one is declared."""
    if table.name == "workspace":
        return ()
    if "workspace_id" in table.c:
        return ((table.c.workspace_id, Base.metadata.tables["workspace"].c.id),)
    if table.name in seen:
        return None
    candidates = []
    for fk in sorted(table.foreign_keys, key=lambda f: (f.column.table.name, f.parent.name)):
        parent = fk.column.table
        path = _workspace_path(parent, seen | {table.name})
        if path is not None:
            candidates.append(((fk.parent, fk.column), *path))
    return min(candidates, key=lambda p: (len(p), str(p))) if candidates else None


def _row_counts(session: Session, workspace_id: str) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {}
    checked = 0
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        path = _workspace_path(table)
        if path is None:
            continue  # platform-wide tables and rows with no declared ownership route
        checked += 1
        if not path:
            stmt = select(func.count()).select_from(table).where(table.c.id == workspace_id)
        else:
            stmt = select(func.count()).select_from(table)
            current = table
            for local, remote in path:
                parent = remote.table
                if parent is not current:
                    stmt = stmt.join(parent, local == remote)
                current = parent
            stmt = stmt.where(Base.metadata.tables["workspace"].c.id == workspace_id)
        n = session.scalar(stmt) or 0
        if n:
            counts[table.name] = n
    return counts, checked


def _location(path: Path) -> dict[str, Any]:
    return {"path": str(path), "exists": path.exists()}


def inventory(session: Session, user: User, workspace_id: str) -> dict[str, Any]:
    """Only an owner may see retained rows and resource identifiers, including after archival."""
    require_role(session, user, workspace_id, "owner", allow_disabled=True, allow_archived=True)
    workspace = get_workspace(session, workspace_id, allow_disabled=True, allow_archived=True)
    counts, checked = _row_counts(session, workspace_id)
    sources = list(session.scalars(select(Source).where(Source.workspace_id == workspace_id).order_by(Source.id)))
    targets = list(session.scalars(select(BuildTarget).where(BuildTarget.workspace_id == workspace_id).order_by(BuildTarget.id)))
    jobs = list(session.scalars(select(BuildJob.id).where(BuildJob.workspace_id == workspace_id).order_by(BuildJob.id)))
    publications = list(session.scalars(select(Publication).where(Publication.workspace_id == workspace_id)
                                        .order_by(Publication.id)))
    settings = get_settings()
    return {
        "workspace_id": workspace_id,
        "status": workspace.status,
        "archived": workspace.deleted_at is not None,
        "control_rows": counts,
        "control_tables_checked": checked,
        "staged_schemas": sorted({s.staging_schema or staging_schema_for(s.id) for s in sources
                                  if s.execution_mode == "staged"}),
        "build_targets": [{"engine": t.engine, "schema": t.schema_name, "role": t.build_role,
                           "status": t.status} for t in targets],
        "local_paths": [_location(Path(settings.upload_dir) / workspace_id),
                        _location(Path(settings.artifact_dir) / workspace_id),
                        *[_location(Path(settings.build_dir) / job_id) for job_id in jobs]],
        "publications": [{"id": p.id, "destination": p.destination, "status": p.status,
                          "external_ids": p.external_ids or {}} for p in publications],
        "verification": "Registry-derived inventory only; remote schemas, files, BI objects, graph, caches, "
                        "workflow histories and backups have not been checked or erased.",
    }
