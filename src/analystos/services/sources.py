"""Source registration -> discovery -> asset selection -> sync (staged snapshot or pushdown ready)."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.config import get_settings
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import require_role

_CREDENTIAL_KEY = re.compile(r"pass(word|wd|phrase)?|pwd|secret|token|api_?key|private_?key|credential|auth", re.I)
_URL_USERINFO = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def _credential_in_config(config: dict, depth: int = 0) -> bool:
    """Any key that names a credential (any case/prefix) or any value embedding user:password@ in a URL."""
    for k, v in (config or {}).items():
        if _CREDENTIAL_KEY.search(str(k)):
            return True
        if isinstance(v, str) and _URL_USERINFO.search(v):
            return True
        if isinstance(v, dict) and depth < 3 and _credential_in_config(v, depth + 1):
            return True
    return False


def register_source(session: Session, user: User, workspace_id: str, *, kind: str, name: str, config: dict,
                    secret_ref: str | None) -> Source:
    """Any kind in config/source_kinds.yaml that the administrator has enabled. Pushdown only where the
    kind supports a read-only session in a dialect the gateway validates, and the admin allows it."""
    from analystos.connectors import kinds
    from analystos.services.platform_settings import get as platform

    require_role(session, user, workspace_id, "editor")
    spec = kinds.get_kind(kind)
    settings = platform().sources
    if settings.enabled_kinds and spec.kind not in settings.enabled_kinds:
        raise InvalidInput(f"source kind '{spec.kind}' is disabled by the administrator")
    config = dict(config or {})
    if _credential_in_config(config):
        raise InvalidInput("put credentials in a secret reference (env:NAME or file:/path), never in source config")
    missing = [f for f in spec.required if config.get(f) in (None, "")]
    if missing:
        raise InvalidInput(f"{spec.label} needs: {', '.join(missing)}")
    if spec.secret and spec.secret.required and not secret_ref:
        raise InvalidInput(f"{spec.label} needs a secret reference (env:NAME or file:/path) for its {spec.secret.field}")
    mode = kinds.execution_mode_for(spec.kind, config.pop("execution_mode", None) if settings.allow_pushdown else "staged")
    src = Source(id=new_id("src"), workspace_id=workspace_id, kind=spec.kind, name=name, config=config, secret_ref=secret_ref,
                 status="registered", execution_mode=mode)
    session.add(src)
    audit(f"user:{user.id}", "source.registered", workspace_id=workspace_id, target=src.id,
          details={"kind": spec.kind, "execution_mode": mode}, session=session)
    return src


def _source(session: Session, user: User, source_id: str, minimum: str = "editor") -> Source:
    src = session.get(Source, source_id)
    if src is None:
        raise NotFound(f"source {source_id} not found")
    require_role(session, user, src.workspace_id, minimum)
    return src


def discover_source(user: User, source_id: str) -> dict:
    """Full metadata crawl without profiling (one code path with scheduled crawls).

    The crawler keeps owner tags and reviewed descriptions; the previous implementation replaced
    every column row on re-discovery, which silently dropped "restricted" tags."""
    from analystos.services.crawler import crawl_source

    with session_scope() as s:
        _source(s, user, source_id)
    result = crawl_source(user, source_id, mode="full", profile=False)
    with session_scope() as s:
        row = s.get(Source, source_id)
        assets = []
        for a in s.scalars(select(SourceAsset).where(SourceAsset.source_id == source_id, SourceAsset.lifecycle == "active")
                           .order_by(SourceAsset.name)):
            cols = s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal))
            assets.append({"source_name": a.source_name, "name": a.name, "schema_name": a.schema_name, "kind": a.kind,
                           "row_count": a.row_count, "description": a.description, "business_name": a.business_name,
                           "freshness_at": a.freshness_at, "semantics": a.semantics,
                           "columns": [{"name": c.name, "data_type": c.data_type, "nullable": c.nullable, "is_key": c.is_key,
                                        "description": c.description, "business_name": c.business_name, "tags": c.tags,
                                        "references": (c.profile or {}).get("references")} for c in cols]})
        emit(row.workspace_id, "source.connected", {"source_id": source_id, "assets": len(assets),
                                                    "latency_ms": result["stats"].get("latency_ms")},
             actor=f"user:{user.id}", session=s)
    return {"assets": assets, "crawl_id": result["crawl_id"], "stats": result["stats"], "changes": result["changes"],
            "test": {"ok": True, "message": "ok", "latency_ms": result["stats"].get("latency_ms", 0)}}


def select_assets(user: User, source_id: str, asset_names: list[str]) -> dict:
    """Mark the assets the workspace may analyse, then sync them (staged: bounded snapshot load)."""
    from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
    from analystos.connectors.registry import build_connector
    from analystos.staging.loader import StagingLoader

    with session_scope() as s:
        src = _source(s, user, source_id)
        assets = list(s.scalars(select(SourceAsset).where(SourceAsset.source_id == source_id)))
        wanted = set(asset_names)
        unknown = wanted - {a.name for a in assets} - {a.source_name for a in assets}
        if unknown:
            raise InvalidInput(f"unknown assets: {', '.join(sorted(unknown))}")
        for a in assets:
            a.selected = a.name in wanted or a.source_name in wanted
        selected = [(a.id, a.source_name, a.name, a.schema_name,
                     [DiscoveredColumn(name=c.name, data_type=c.data_type, nullable=c.nullable, is_key=c.is_key,
                                       references=(c.profile or {}).get("references"))
                      for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal))])
                    for a in assets if a.selected]
        s.expunge(src)
    loaded = []
    if src.execution_mode == "staged":
        from analystos.services.platform_settings import get as platform

        platform_max = platform().sources.staged_max_rows
        settings = get_settings()
        connector = build_connector(src, settings)
        loader = StagingLoader(settings)
        for asset_id, source_name, name, schema, cols in selected:
            d = DiscoveredAsset(source_name=source_name, name=name, columns=cols, kind="api_table")
            max_rows = min(int(src.config.get("max_rows") or platform_max), platform_max)
            info = loader.load(source_id, d, connector.extract(d, max_rows=max_rows))
            loaded.append({"asset": f"{schema}.{name}", **info})
            with session_scope() as s:
                a = s.get(SourceAsset, asset_id)
                a.row_count, a.freshness_at = info.get("row_count"), utcnow()
    with session_scope() as s:
        row = s.get(Source, source_id)
        row.status = "ready"
        row.last_discovered_at = utcnow()  # bumps the gateway cache's source version after a re-stage
        audit(f"user:{user.id}", "source.assets_selected", workspace_id=row.workspace_id, target=source_id,
              details={"assets": asset_names, "loaded": loaded}, session=s)
        emit(row.workspace_id, "metadata.collected", {"source_id": source_id, "selected": asset_names, "loaded": loaded},
             actor=f"user:{user.id}", session=s)
    return {"selected": asset_names, "loaded": loaded}


def tag_column(session: Session, user: User, asset_id: str, column: str, tags: list[str]) -> SourceColumn:
    asset = session.get(SourceAsset, asset_id)
    if asset is None:
        raise NotFound("asset not found")
    require_role(session, user, asset.workspace_id, "owner")
    col = session.scalar(select(SourceColumn).where(SourceColumn.asset_id == asset_id, SourceColumn.name == column))
    if col is None:
        raise NotFound("column not found")
    allowed = {"pii", "restricted", "sensitive"}
    if set(tags) - allowed:
        raise InvalidInput(f"tags must be within {sorted(allowed)}")
    col.tags, col.tags_origin = sorted(set(tags)), "user"
    audit(f"user:{user.id}", "column.tagged", workspace_id=asset.workspace_id, target=f"{asset.schema_name}.{asset.name}.{column}",
          details={"tags": col.tags}, session=session)
    return col
