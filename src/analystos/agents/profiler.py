"""Dataset Profiler + Data Quality agents (§13.4, §13.5)."""
from __future__ import annotations

from sqlalchemy import select

from analystos.agents.common import asset_rows, task_output
from analystos.artifacts.registry import link, save_artifact
from analystos.db.base import session_scope
from analystos.db.models import Artifact, SourceAsset, SourceColumn
from analystos.runtime.context import RunContext


def _as_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return dict(vars(value))


def profile_tables(ctx: RunContext) -> dict:
    from analystos.skills.profiling import profile_asset

    summaries = []
    for asset, cols in asset_rows(ctx):
        fq = f"{asset.schema_name}.{asset.name}"
        run_sql = ctx.run_sql(ctx.scope.asset_sources[fq])
        visible = [{"name": c.name, "data_type": c.data_type} for c in cols if f"{fq}.{c.name}" not in ctx.scope.denied_columns]
        ctx.check_control()
        profile = _as_dict(ctx.tools().invoke("profile.table", {"asset": fq, "columns": len(visible)},
                                              lambda fq=fq, visible=visible, run_sql=run_sql: profile_asset(run_sql, fq, visible)))
        col_profiles = {c["name"]: c for c in (profile.get("columns") or [])} if isinstance(profile.get("columns"), list) \
            else (profile.get("columns") or {})
        with session_scope() as s:
            for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id)):
                p = col_profiles.get(c.name)
                if p:
                    p = _as_dict(p)
                    refs = (c.profile or {}).get("references")
                    c.profile = {**p, **({"references": refs} if refs else {})}
                    c.semantic_type = p.get("semantic_type") or c.semantic_type
            row = s.get(SourceAsset, asset.id)
            row.stats = {k: v for k, v in profile.items() if k != "columns"}
            if profile.get("row_count") is not None:
                row.row_count = int(profile["row_count"])
            art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="profile",
                                name=f"Profile {fq}", content=profile, creator_agent=ctx.agent.id)
            link(s, ctx.workspace.id, ("table", fq), "profiled_by", ("artifact", art.id), run_id=ctx.run.id)
        summaries.append({"asset": fq, "artifact_id": art.id, "row_count": profile.get("row_count"),
                          "columns": len(col_profiles),
                          "semantic_types": {n: _as_dict(p).get("semantic_type") for n, p in col_profiles.items()}})
        ctx.event("profile.completed", {"asset": fq, "row_count": profile.get("row_count"), "columns": len(col_profiles)})
    ctx.say("Profiled " + ", ".join(f"{s['asset']} ({s['columns']} columns)" for s in summaries) + ".")
    return {"tables": summaries}


def check_quality(ctx: RunContext) -> dict:
    from analystos.skills.quality import check_quality as run_checks

    rels = task_output(ctx.run.id, "relationships").get("relationships") or []
    issues_all = []
    for asset, _cols in asset_rows(ctx):
        fq = f"{asset.schema_name}.{asset.name}"
        from analystos.skills.profiling import AssetProfile

        with session_scope() as s:
            art = s.scalar(select(Artifact).where(Artifact.run_id == ctx.run.id, Artifact.type == "profile",
                                                  Artifact.name == f"Profile {fq}"))
            if art is None:
                continue
            art_profile = AssetProfile.model_validate(art.content)
        my_rels = [r for r in rels if r.get("from_asset") == fq]
        run_sql = ctx.run_sql(ctx.scope.asset_sources[fq])
        issues = ctx.tools().invoke("quality.check", {"asset": fq}, lambda fq=fq, p=art_profile, r=my_rels, run_sql=run_sql: run_checks(run_sql, fq, p, r))
        for issue in issues:
            d = _as_dict(issue)
            issues_all.append(d)
            if d.get("severity") in ("warning", "critical"):
                ctx.event("quality.issue.detected", {"asset": fq, "code": d.get("code"), "severity": d.get("severity"),
                                                     "message": d.get("message")})
    with session_scope() as s:
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="quality_report",
                            name="Data quality report", content={"issues": issues_all}, creator_agent=ctx.agent.id)
        for fq in ctx.scope.assets:
            link(s, ctx.workspace.id, ("table", fq), "quality_checked_by", ("artifact", art.id), run_id=ctx.run.id)
    crit = [i for i in issues_all if i.get("severity") == "critical"]
    warn = [i for i in issues_all if i.get("severity") == "warning"]
    ctx.say(f"Data quality: {len(crit)} critical, {len(warn)} warnings, {len(issues_all) - len(crit) - len(warn)} info. "
            + " ".join(f"[{i.get('severity')}] {i.get('message')}" for i in (crit + warn)[:5]))
    return {"issues": issues_all, "artifact_id": art.id}
