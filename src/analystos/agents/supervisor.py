"""Analytics Supervisor: plan framing, plan-approval gate and run consolidation."""
from __future__ import annotations

import re

from sqlalchemy import select

from analystos.agents.common import catalog_for_prompt, llm_json
from analystos.artifacts.registry import link, save_artifact
from analystos.context.service import add_entry
from analystos.core.ids import utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Insight, RunTask
from analystos.events.bus import emit
from analystos.graph.projection import project_workspace
from analystos.runtime.context import RunContext, Services
from analystos.runtime.plan import base_plan


def build_plan(run_id: str, services: Services) -> dict:
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        objective, level, instructions = run.objective, run.autonomy_level, run.instructions
    framing: dict = {}
    # Framing needs the catalog, which needs a context; build a light pseudo-context for the supervisor.
    try:
        ctx = _supervisor_ctx(run_id, services)
        data, model = llm_json(ctx, "planning", "planning.v1", {
            "objective": objective, "user_instructions": [i.get("text") for i in instructions],
            "catalog": catalog_for_prompt(ctx, include_values=False)})
        if isinstance(data, dict):
            framing = data
            ctx.say(f"Framed the objective into {len(data.get('questions') or [])} analytical questions.", kind="decision",
                    data={"model": model, "questions": data.get("questions")})
    except Exception:  # planning framing is optional; the lifecycle skeleton is always valid
        framing = {}
    questions = [str(q) for q in (framing.get("questions") or [])][:8]
    audience = [a for a in (framing.get("audience") or ["executive", "operational"]) if a in ("executive", "operational")]
    return base_plan(objective, autonomy_level=level, questions=questions, audience=audience or ["executive", "operational"],
                     focus=[str(f) for f in (framing.get("focus") or [])][:6])


def _supervisor_ctx(run_id: str, services: Services) -> RunContext:
    """A RunContext for planning before tasks exist (uses a transient task row)."""
    from analystos.core.ids import new_id

    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == "_planning"))
        if task is None:
            task = RunTask(id=new_id("tsk"), run_id=run_id, key="_planning", agent_id="supervisor", title="Planning",
                           status="COMPLETED", plan_version=0, seq=-1, input={"optional": True})
            s.add(task)
    return RunContext.load(run_id, "_planning", services)


def plan_approved(ctx: RunContext) -> dict:
    with session_scope() as s:
        approval = s.get(Approval, ctx.task.input.get("approval_id"))
        if approval and approval.status == "approved":
            approval.status = "executed"
    return {"approved": True}


_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def finalize(ctx: RunContext) -> dict:
    with session_scope() as s:
        insights = list(s.scalars(select(Insight).where(Insight.run_id == ctx.run.id, Insight.status == "verified")))
        facts = [{"code": i.code, "title": i.title, "finding": i.finding, "confidence": i.confidence,
                  "impact": i.business_impact} for i in insights]
    summary_md, source = None, "template"
    data, model = llm_json(ctx, "summarization", "run_summary.v1", {"objective": ctx.run.objective, "facts": facts})
    if isinstance(data, dict) and isinstance(data.get("summary_markdown"), str):
        allowed = {n for f in facts for n in _NUM.findall(str(f))}
        used = set(_NUM.findall(data["summary_markdown"]))
        if used <= allowed | {str(i) for i in range(0, 11)}:
            summary_md, source = data["summary_markdown"], f"llm:{model}"
    if summary_md is None:
        summary_md = "\n".join(f"- **{f['title']}** — {f['finding']}" for f in facts) or "- No finding passed verification."
    with session_scope() as s:
        run = s.get(AnalysisRun, ctx.run.id)
        tasks = {t.key: t for t in s.scalars(select(RunTask).where(RunTask.run_id == ctx.run.id))}
        published = tasks.get("publish") and tasks["publish"].status == "COMPLETED"
        run.summary = {**(run.summary or {}), "summary_markdown": summary_md, "summary_source": source,
                       "verified_insights": len(facts), "published": bool(published),
                       "publication": (tasks["publish"].output if published else None)}
        add_entry(s, workspace_id=ctx.workspace.id, kind="episode", name=f"Run {run.id}: {run.objective[:120]}",
                  body=summary_md, origin="agent")
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="narrative",
                            name="Executive summary", content={"markdown": summary_md, "source": source},
                            creator_agent="supervisor", status="final")
        link(s, ctx.workspace.id, ("run", ctx.run.id), "summarized_by", ("artifact", art.id), run_id=ctx.run.id)
        for i in facts:
            link(s, ctx.workspace.id, ("artifact", art.id), "cites", ("insight", i["code"]), run_id=ctx.run.id)
        graph = project_workspace(s, ctx.workspace.id)
        run.summary = {**run.summary, "graph_projection": graph}
        emit(ctx.workspace.id, "analysis.completed", {"verified_insights": len(facts), "published": bool(published)},
             run_id=ctx.run.id, session=s)
        run.finished_at = utcnow()
    return {"verified_insights": len(facts), "published": bool(published), "graph": graph}
