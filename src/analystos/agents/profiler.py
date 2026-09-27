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


def _stored_profile(asset: SourceAsset, visible: list[SourceColumn]) -> dict | None:
    """The persisted profile as an AssetProfile document, when it still describes the table (same structural
    fingerprint and staged load, younger than crawl.profile_reuse_hours) and covers every visible column."""
    from analystos.services.crawler import profile_reusable
    from analystos.services.platform_settings import get as platform

    if not profile_reusable(asset, platform().crawl.profile_reuse_hours):
        return None
    cols = [c.profile for c in visible if (c.profile or {}).get("type_family")]
    if len(cols) != len(visible) or "dialect" not in (asset.stats or {}):
        return None
    return {**{k: v for k, v in (asset.stats or {}).items() if k not in ("profile_meta", "key_check")},
            "columns": [dict(c) for c in cols], "reused": True, "profile_meta": asset.stats.get("profile_meta")}


def profile_tables(ctx: RunContext) -> dict:
    """Profile every scoped table, or reuse its persisted profile when it still describes the table (recorded as
    `reused` in the artifact). Declared keys and references reach the profiler; a sensitive column is profiled for
    counts only and its stored and shown profile keeps counts only (the crawler's sanitizer)."""
    from analystos.services.crawler import persist_profile
    from analystos.skills.profiling import column_is_sensitive, profile_asset, sanitize_column_profile

    summaries = []
    for asset, cols in asset_rows(ctx):
        fq = f"{asset.schema_name}.{asset.name}"
        visible_cols = [c for c in cols if f"{fq}.{c.name}" not in ctx.scope.denied_columns]
        profile = _stored_profile(asset, visible_cols)
        if profile is None:
            run_sql = ctx.run_sql(ctx.scope.asset_sources[fq])
            visible = [{"name": c.name, "data_type": c.data_type, "is_key": c.is_key,
                        "references": (c.profile or {}).get("references"),
                        "sensitive": column_is_sensitive(c.tags, c.semantics)} for c in visible_cols]
            ctx.check_control()
            profile = _as_dict(ctx.tools().invoke("profile.table", {"asset": fq, "columns": len(visible)},
                                                  lambda fq=fq, visible=visible, run_sql=run_sql: profile_asset(run_sql, fq, visible)))
            profile = {**profile, "columns": [_as_dict(p) for p in profile.get("columns") or []], "reused": False}
            with session_scope() as s:
                persist_profile(s, asset.id, profile)
        sensitive = {c.name for c in visible_cols if column_is_sensitive(c.tags, c.semantics)}
        profile = {**profile, "columns": [sanitize_column_profile(p, sensitive=p.get("name") in sensitive)
                                          for p in profile.get("columns") or []]}
        col_profiles = {c["name"]: c for c in profile["columns"]}
        with session_scope() as s:
            art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="profile",
                                name=f"Profile {fq}", content=profile, creator_agent=ctx.agent.id)
            link(s, ctx.workspace.id, ("table", fq), "profiled_by", ("artifact", art.id), run_id=ctx.run.id)
        summaries.append({"asset": fq, "artifact_id": art.id, "row_count": profile.get("row_count"),
                          "columns": len(col_profiles), "reused": profile["reused"],
                          "semantic_types": {n: p.get("semantic_type") for n, p in col_profiles.items()}})
        ctx.event("profile.completed", {"asset": fq, "row_count": profile.get("row_count"), "columns": len(col_profiles),
                                        "reused": profile["reused"]})
    ctx.say("Profiled " + ", ".join(f"{s['asset']} ({s['columns']} columns{', reused' if s['reused'] else ''})"
                                    for s in summaries) + ".")
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
