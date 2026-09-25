"""Source registration -> discovery -> asset selection -> sync (staged snapshot or pushdown ready)."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from analystos.core.config import get_settings
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import require_role

KINDS = {"postgres", "sqlserver", "csv", "servicenow"}
PII_HINTS = ("email", "phone", "caller", "ssn", "address", "birth", "first_name", "last_name", "user_name", "mobile")


def register_source(session: Session, user: User, workspace_id: str, *, kind: str, name: str, config: dict,
                    secret_ref: str | None) -> Source:
    require_role(session, user, workspace_id, "editor")
    if kind not in KINDS:
        raise InvalidInput(f"kind must be one of {sorted(KINDS)}")
    if any(k in (config or {}) for k in ("password", "secret", "token", "api_key")):
        raise InvalidInput("put credentials in a secret reference (env:NAME or file:/path), never in source config")
    src = Source(id=new_id("src"), workspace_id=workspace_id, kind=kind, name=name, config=config or {}, secret_ref=secret_ref,
                 status="registered", execution_mode="pushdown" if kind in ("postgres", "sqlserver") else "staged")
    session.add(src)
    audit(f"user:{user.id}", "source.registered", workspace_id=workspace_id, target=src.id, details={"kind": kind}, session=session)
    return src


def _source(session: Session, user: User, source_id: str, minimum: str = "editor") -> Source:
    src = session.get(Source, source_id)
    if src is None:
        raise NotFound(f"source {source_id} not found")
    require_role(session, user, src.workspace_id, minimum)
    return src


def discover_source(user: User, source_id: str) -> dict:
    from analystos.connectors.registry import build_connector

    with session_scope() as s:
        src = _source(s, user, source_id)
        s.expunge(src)
    connector = build_connector(src, get_settings())
    test = connector.test()
    if not test.ok:
        with session_scope() as s:
            row = s.get(Source, source_id)
            row.status, row.last_error = "error", test.message
        raise InvalidInput(f"connection failed: {test.message}")
    assets = connector.discover()
    from analystos.connectors.naming import staging_schema_for

    schema_for_staged = staging_schema_for(source_id)
    with session_scope() as s:
        row = s.get(Source, source_id)
        existing = {(a.source_name): a for a in s.scalars(select(SourceAsset).where(SourceAsset.source_id == source_id))}
        for a in assets:
            schema = schema_for_staged if row.execution_mode == "staged" else (a.schema_name or "public")
            asset = existing.get(a.source_name)
            if asset is None:
                asset = SourceAsset(id=new_id("ast"), source_id=source_id, workspace_id=row.workspace_id, schema_name=schema,
                                    name=a.name, source_name=a.source_name, kind=a.kind, selected=False)
                s.add(asset)
                s.flush()
            asset.row_count, asset.description, asset.business_name = a.row_count, a.description, a.business_name
            asset.freshness_at = a.freshness_at
            s.execute(delete(SourceColumn).where(SourceColumn.asset_id == asset.id))
            for i, c in enumerate(a.columns):
                tags = ["pii"] if any(h in c.name.lower() for h in PII_HINTS) and not c.name.endswith("_name") else []
                s.add(SourceColumn(asset_id=asset.id, name=c.name, ordinal=i, data_type=c.data_type, nullable=c.nullable,
                                   is_key=c.is_key, business_name=c.business_name, description=c.description, tags=tags,
                                   profile={"references": c.references} if c.references else {}))
        row.status, row.staging_schema, row.last_discovered_at, row.last_error = "discovered", \
            schema_for_staged if row.execution_mode == "staged" else None, utcnow(), None
        emit(row.workspace_id, "source.connected", {"source_id": source_id, "assets": len(assets), "latency_ms": test.latency_ms},
             actor=f"user:{user.id}", session=s)
    return {"assets": [a.model_dump(mode="json") for a in assets], "test": test.model_dump()}


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
        settings = get_settings()
        connector = build_connector(src, settings)
        loader = StagingLoader(settings)
        for asset_id, source_name, name, schema, cols in selected:
            d = DiscoveredAsset(source_name=source_name, name=name, columns=cols, kind="api_table")
            max_rows = int(src.config.get("max_rows") or 1_000_000)
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
    col.tags = sorted(set(tags))
    audit(f"user:{user.id}", "column.tagged", workspace_id=asset.workspace_id, target=f"{asset.schema_name}.{asset.name}.{column}",
          details={"tags": col.tags}, session=session)
    return col
