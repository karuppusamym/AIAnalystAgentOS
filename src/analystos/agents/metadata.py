"""Metadata Agent: technical metadata + relationship discovery (§13.3, §13.6)."""
from __future__ import annotations

from sqlalchemy import select

from analystos.agents.common import asset_rows
from analystos.artifacts.registry import link, save_artifact
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Relationship, SourceAsset
from analystos.runtime.context import RunContext


def collect_metadata(ctx: RunContext) -> dict:
    tables = []
    for asset, cols in asset_rows(ctx):
        fq = f"{asset.schema_name}.{asset.name}"
        run_sql = ctx.run_sql(ctx.scope.asset_sources[fq])

        def count(fq=fq, run_sql=run_sql):
            return run_sql(f"SELECT COUNT(*) AS n FROM {fq}", purpose="metadata.row_count").rows[0][0]

        n = ctx.tools().invoke("metadata.read", {"asset": fq}, count)
        with session_scope() as s:
            row = s.get(SourceAsset, asset.id)
            row.row_count = int(n)
        tables.append({"asset": fq, "row_count": int(n), "columns": [
            {"name": c.name, "type": c.data_type, "key": c.is_key, "tags": c.tags} for c in cols],
            "denied_columns": [c.name for c in cols if f"{fq}.{c.name}" in ctx.scope.denied_columns]})
    with session_scope() as s:
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="profile",
                            name="Technical metadata", content={"tables": tables}, creator_agent=ctx.agent.id)
        for t in tables:
            link(s, ctx.workspace.id, ("table", t["asset"]), "described_by", ("artifact", art.id), run_id=ctx.run.id)
    ctx.event("metadata.collected", {"tables": len(tables), "rows": sum(t["row_count"] for t in tables)})
    ctx.say("Metadata: " + "; ".join(f"{t['asset']} ({t['row_count']:,} rows, {len(t['columns'])} columns"
                                     + (f", {len(t['denied_columns'])} restricted" if t["denied_columns"] else "") + ")"
                                     for t in tables))
    return {"tables": tables}


def discover_relationships(ctx: RunContext) -> dict:
    from analystos.skills.relationships import discover_relationships as discover

    run_sql = ctx.run_sql(next(iter(ctx.scope.asset_sources.values())))
    assets = []
    ids = {}
    for asset, cols in asset_rows(ctx):
        fq = f"{asset.schema_name}.{asset.name}"
        ids[fq] = asset.id
        assets.append({"asset": fq, "row_count": asset.row_count, "columns": [
            {"name": c.name, "data_type": c.data_type, "is_key": c.is_key, "references": (c.profile or {}).get("references")}
            for c in cols if f"{fq}.{c.name}" not in ctx.scope.denied_columns]})
    candidates = ctx.tools().invoke("relationships.discover", {"assets": list(ids)}, lambda: discover(run_sql, assets))
    out = []
    with session_scope() as s:
        for c in candidates:
            c = c if isinstance(c, dict) else c.model_dump() if hasattr(c, "model_dump") else vars(c)
            if c["from_asset"] not in ids or c["to_asset"] not in ids:
                continue
            existing = s.scalar(select(Relationship).where(
                Relationship.workspace_id == ctx.workspace.id, Relationship.from_asset_id == ids[c["from_asset"]],
                Relationship.from_column == c["from_column"], Relationship.to_asset_id == ids[c["to_asset"]],
                Relationship.to_column == c["to_column"]))
            row = existing or Relationship(id=new_id("rel"), workspace_id=ctx.workspace.id, from_asset_id=ids[c["from_asset"]],
                                           from_column=c["from_column"], to_asset_id=ids[c["to_asset"]], to_column=c["to_column"])
            row.cardinality = c.get("cardinality", "many_to_one")
            row.confidence = float(c.get("confidence", 0))
            row.validated = float(c.get("confidence", 0)) >= 0.8
            row.evidence = c.get("evidence") or {}
            s.add(row)
            out.append(c)
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="relationship_map",
                            name="Relationship map", content={"relationships": out}, creator_agent=ctx.agent.id)
        for c in out:
            link(s, ctx.workspace.id, ("table", c["from_asset"]), "joins_to", ("table", c["to_asset"]), run_id=ctx.run.id)
    for c in out:
        ctx.event("relationship.discovered", {"from": f"{c['from_asset']}.{c['from_column']}",
                                              "to": f"{c['to_asset']}.{c['to_column']}", "confidence": c.get("confidence")})
    ctx.say(f"Discovered {len(out)} relationships" + (": " + "; ".join(
        f"{c['from_asset']}.{c['from_column']} → {c['to_asset']}.{c['to_column']} ({c.get('cardinality')}, "
        f"conf {float(c.get('confidence', 0)):.2f})" for c in out[:6]) if out else "."))
    return {"relationships": out, "artifact_id": art.id}
