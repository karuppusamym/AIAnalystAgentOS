from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.api.serialize import rows
from analystos.context import service as ctx_svc
from analystos.contracts.registry import AgentSpec, ToolSpec
from analystos.core.errors import NotFound
from analystos.db.models import (
    AgentDefinition,
    AuditEvent,
    ContextEntry,
    ModelCall,
    QueryExecution,
    SkillDefinition,
    ToolDefinition,
    User,
)
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.llm.config import load_models_config
from analystos.tools.registry import list_tools

router = APIRouter(prefix="/api", tags=["admin"])


class SearchIn(BaseModel):
    workspace_id: str
    query: str
    limit: int = 8


class ContextIn(BaseModel):
    kind: str = "term"
    name: str
    body: str
    synonyms: list[str] = []
    mapped_columns: list[str] = []


class EnabledPatch(BaseModel):
    enabled: bool | None = None
    spec: dict | None = None


@router.post("/context/search")
def context_search(body: SearchIn, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, body.workspace_id, "viewer")
    return ctx_svc.search(session, body.workspace_id, body.query, limit=min(body.limit, 50))


@router.get("/context/entities/{entry_id}")
def context_entity(entry_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    e = session.get(ContextEntry, entry_id)
    if e is None:
        raise NotFound("context entry not found")
    if e.workspace_id:
        require_role(session, user, e.workspace_id, "viewer")
    return {"id": e.id, "kind": e.kind, "name": e.name, "body": e.body, "synonyms": e.synonyms, "mapped_columns": e.mapped_columns,
            "origin": e.origin, "workspace_id": e.workspace_id}


@router.post("/workspaces/{workspace_id}/context")
def add_context(workspace_id: str, body: ContextIn, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "editor")
    e = ctx_svc.add_entry(session, workspace_id=workspace_id, **body.model_dump(), origin="user")
    return {"id": e.id}


@router.get("/workspaces/{workspace_id}/context")
def list_context(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db)):
    require_role(session, user, workspace_id, "viewer")
    return [{"id": e.id, "kind": e.kind, "name": e.name, "body": e.body, "synonyms": e.synonyms, "mapped_columns": e.mapped_columns,
             "origin": e.origin, "global": e.workspace_id is None}
            for e in session.scalars(select(ContextEntry).where((ContextEntry.workspace_id == workspace_id) | ContextEntry.workspace_id.is_(None))
                                     .where(ContextEntry.kind != "episode").order_by(ContextEntry.kind, ContextEntry.name))]


@router.get("/tools")
def tools(_: User = Depends(current_user), session: Session = Depends(db)):
    return list_tools(session)


@router.post("/tools")
def register_tool(body: dict, admin: User = Depends(admin_user), session: Session = Depends(db)):
    spec = ToolSpec.model_validate(body)
    session.merge(ToolDefinition(id=spec.tool_id, spec=spec.model_dump(), enabled=False))
    audit(f"user:{admin.id}", "tool.registered", target=spec.tool_id, session=session)
    return {"tool_id": spec.tool_id, "enabled": False, "note": "new tools start disabled; enable after review"}


@router.patch("/tools/{tool_id}")
def patch_tool(tool_id: str, body: EnabledPatch, admin: User = Depends(admin_user), session: Session = Depends(db)):
    t = session.get(ToolDefinition, tool_id)
    if t is None:
        raise NotFound("tool not found")
    if body.enabled is not None:
        t.enabled = body.enabled
    if body.spec:
        t.spec = ToolSpec.model_validate({**t.spec, **body.spec}).model_dump()
    audit(f"user:{admin.id}", "tool.updated", target=tool_id, details=body.model_dump(), session=session)
    return {**t.spec, "enabled": t.enabled}


@router.get("/agents")
def agents(_: User = Depends(current_user), session: Session = Depends(db)):
    return [{**a.spec, "enabled": a.enabled} for a in session.scalars(select(AgentDefinition).order_by(AgentDefinition.id))]


@router.post("/agents")
def register_agent(body: dict, admin: User = Depends(admin_user), session: Session = Depends(db)):
    spec = AgentSpec.model_validate(body)
    session.merge(AgentDefinition(id=spec.id, version=spec.version, spec=spec.model_dump(), enabled=False))
    audit(f"user:{admin.id}", "agent.registered", target=spec.id, session=session)
    return {"id": spec.id, "enabled": False}


@router.patch("/agents/{agent_id}")
def patch_agent(agent_id: str, body: EnabledPatch, admin: User = Depends(admin_user), session: Session = Depends(db)):
    a = session.get(AgentDefinition, agent_id)
    if a is None:
        raise NotFound("agent not found")
    if body.enabled is not None:
        a.enabled = body.enabled
    if body.spec:
        a.spec = AgentSpec.model_validate({**a.spec, **body.spec}).model_dump()
    audit(f"user:{admin.id}", "agent.updated", target=agent_id, details=body.model_dump(), session=session)
    return {**a.spec, "enabled": a.enabled}


@router.get("/skills")
def skills(_: User = Depends(current_user), session: Session = Depends(db)):
    return [{**s.spec, "enabled": s.enabled} for s in session.scalars(select(SkillDefinition).order_by(SkillDefinition.id))]


@router.get("/admin/models")
def models(_: User = Depends(current_user)):
    from analystos.runtime.context import default_router

    cfg = load_models_config()
    router_ = default_router()
    return {**cfg.public_view(), "available": {p: router_.available(p) for p in cfg.routing}}


@router.get("/admin/usage")
def usage(_: User = Depends(admin_user), session: Session = Depends(db)):
    by_model = session.execute(select(ModelCall.purpose, ModelCall.model, ModelCall.provider, func.count(), func.sum(ModelCall.cost_usd),
                                      func.avg(ModelCall.latency_ms), func.count().filter(ModelCall.status != "ok"))
                               .group_by(ModelCall.purpose, ModelCall.model, ModelCall.provider)).all()
    q = session.execute(select(QueryExecution.status, func.count(), func.avg(QueryExecution.duration_ms)).group_by(QueryExecution.status)).all()
    return {"models": [{"purpose": p, "model": m, "provider": pr, "calls": c, "cost_usd": float(cost or 0), "avg_latency_ms": float(lat or 0),
                        "failed": int(f or 0)} for p, m, pr, c, cost, lat, f in by_model],
            "queries": [{"status": s, "count": c, "avg_ms": float(a or 0)} for s, c, a in q]}


@router.get("/admin/audit")
def admin_audit(limit: int = 300, _: User = Depends(admin_user), session: Session = Depends(db)):
    return rows(session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 2000))))
