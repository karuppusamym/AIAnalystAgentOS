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


def auto_validates(candidate: dict) -> bool:
    """A discovered join is validated without a person only when the source declares it or the measured assessment
    corroborates it (skills/relationships.assess_relationship), and it does not fan out. A name-only match stays
    unvalidated and goes to the review queue, however high its confidence."""
    declared = (candidate.get("evidence") or {}).get("source") == "declared"
    corroborated = bool((candidate.get("assessment") or {}).get("approvable"))
    return (declared or corroborated) and candidate.get("cardinality") in ("many_to_one", "one_to_one") \
        and float((candidate.get("evidence") or {}).get("containment") or 0.0) >= 0.99


def discover_relationships(ctx: RunContext) -> dict:
    """Relationship discovery over every source in the run's scope (one gateway runner per source; joins never cross
    sources). Validation follows `auto_validates`; everything else is recorded unvalidated and queued as a review
    candidate. A relationship a person validated or declared is never demoted by a run."""
    from analystos.semantic.review import observed_joins, record_candidate
    from analystos.skills.relationships import RelationshipCandidate
    from analystos.skills.relationships import discover_relationships as discover

    by_source: dict[str, list[dict]] = {}
    ids: dict[str, str] = {}
    for asset, cols in asset_rows(ctx):
        fq = f"{asset.schema_name}.{asset.name}"
        ids[fq] = asset.id
        by_source.setdefault(ctx.scope.asset_sources[fq], []).append({"asset": fq, "row_count": asset.row_count, "columns": [
            {"name": c.name, "data_type": c.data_type, "is_key": c.is_key, "nullable": c.nullable,
             "references": (c.profile or {}).get("references")}
            for c in cols if f"{fq}.{c.name}" not in ctx.scope.denied_columns]})
    candidates: list = []
    for source_id, group in sorted(by_source.items()):
        run_sql = ctx.run_sql(source_id)
        with session_scope() as s:
            observed = observed_joins(s, ctx.workspace.id, source_id)
        found = ctx.tools().invoke("relationships.discover", {"assets": [a["asset"] for a in group]},
                                   lambda run_sql=run_sql, group=group, observed=observed: discover(run_sql, group, observed=observed))
        candidates += [(source_id, c) for c in found]
    out = []
    queued = 0
    with session_scope() as s:
        for source_id, c in candidates:
            c = c if isinstance(c, dict) else c.model_dump() if hasattr(c, "model_dump") else vars(c)
            if c["from_asset"] not in ids or c["to_asset"] not in ids:
                continue
            existing = s.scalar(select(Relationship).where(
                Relationship.workspace_id == ctx.workspace.id, Relationship.from_asset_id == ids[c["from_asset"]],
                Relationship.from_column == c["from_column"], Relationship.to_asset_id == ids[c["to_asset"]],
                Relationship.to_column == c["to_column"]))
            row = existing or Relationship(id=new_id("rel"), workspace_id=ctx.workspace.id, from_asset_id=ids[c["from_asset"]],
                                           from_column=c["from_column"], to_asset_id=ids[c["to_asset"]], to_column=c["to_column"],
                                           origin="discovered", validated=False)
            decided = existing is not None and (existing.origin in ("user", "review") or existing.validated)
            if not decided:
                row.cardinality = c.get("cardinality", "many_to_one")
                row.confidence = float(c.get("confidence", 0))
                row.validated = auto_validates(c)
            row.evidence = {**(row.evidence or {}), **{k: v for k, v in (c.get("evidence") or {}).items() if k != "sql"},
                            "assessment": (c.get("assessment") or {}).get("outcome"), "run_id": ctx.run.id}
            s.add(row)
            s.flush()
            c = {**c, "validated": row.validated}
            if not row.validated:
                cand = record_candidate(s, ctx.workspace.id, RelationshipCandidate.model_validate(c), source_id=source_id,
                                        origin=f"agent:{ctx.agent.id}"[:80], proposed_by=ctx.run.requested_by or ctx.agent.id)
                queued += int(cand.status == "pending")
                c["candidate_id"] = cand.id
            out.append(c)
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="relationship_map",
                            name="Relationship map", content={"relationships": out}, creator_agent=ctx.agent.id)
        for c in out:
            link(s, ctx.workspace.id, ("table", c["from_asset"]), "joins_to", ("table", c["to_asset"]), run_id=ctx.run.id)
    for c in out:
        ctx.event("relationship.discovered", {"from": f"{c['from_asset']}.{c['from_column']}",
                                              "to": f"{c['to_asset']}.{c['to_column']}", "confidence": c.get("confidence"),
                                              "validated": c["validated"]})
    ctx.say(f"Discovered {len(out)} relationships ({queued} queued for review)" + (": " + "; ".join(
        f"{c['from_asset']}.{c['from_column']} → {c['to_asset']}.{c['to_column']} ({c.get('cardinality')}, "
        f"conf {float(c.get('confidence', 0)):.2f}{', validated' if c['validated'] else ''})" for c in out[:6]) if out else "."))
    return {"relationships": out, "artifact_id": art.id}
