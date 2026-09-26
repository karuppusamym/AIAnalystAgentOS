"""Context Agent: layered retrieval + term resolution (§11.3, §13.2)."""
from __future__ import annotations

from analystos.artifacts.registry import link, save_artifact
from analystos.context.service import build_context_package
from analystos.db.base import session_scope
from analystos.runtime.context import RunContext


def load_context(ctx: RunContext) -> dict:
    notes = [i.get("text", "") for i in ctx.run.instructions if i.get("kind") in ("add_context", "redirect")]

    def retrieve():
        with session_scope() as s:
            return build_context_package(s, ctx.workspace.id, ctx.run.objective, ctx.scope.assets, notes,
                                         user=ctx.user, run_id=ctx.run.id)

    package = ctx.tools().invoke("context.search", {"objective": ctx.run.objective, "assets": ctx.scope.assets}, retrieve)
    # Term resolution: map glossary entries that name columns in scope.
    in_scope = {f"{a}.{c}" for a, cols in ctx.scope.columns.items() for c in cols}
    resolved = []
    for term in package["glossary"]:
        cols = [c for c in term.get("mapped_columns") or [] if any(fq.endswith(c) for fq in in_scope) or c in in_scope]
        if cols and term["score"] > 0.15:
            resolved.append({"term": term["name"], "columns": cols, "definition": term["body"][:300]})
    objective_words = {w for w in ctx.run.objective.lower().replace(",", " ").split() if len(w) > 4}
    ambiguous = sorted(w for w in objective_words if w not in " ".join(package["known_terms"]) and
                       not any(w in t["name"].lower() or w in t["body"].lower() for t in package["glossary"]))[:8]
    package["resolved_terms"] = resolved
    package["ambiguous_terms"] = ambiguous
    with session_scope() as s:
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="context_package",
                            name="Context package", content=package, creator_agent=ctx.agent.id)
        link(s, ctx.workspace.id, ("run", ctx.run.id), "uses_context", ("artifact", art.id), run_id=ctx.run.id)
    ctx.event("context.loaded", {"glossary": len(package["glossary"]), "metrics": len(package["metrics"]),
                                 "graph": len(package["graph"]), "resolved_terms": len(resolved)})
    ctx.say(f"Loaded context: {len(package['glossary'])} glossary entries, {len(package['metrics'])} known metrics, "
            f"{len(resolved)} business terms mapped to columns, {len(package['graph'])} graph links."
            + (f" Unmapped objective terms: {', '.join(ambiguous)}." if ambiguous else ""))
    return {"artifact_id": art.id, "resolved_terms": resolved, "ambiguous_terms": ambiguous}
