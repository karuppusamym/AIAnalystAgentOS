"""Recipe API (P6-04..P6-07, ADR-0023): validate, save, publish, compile, preview, run, and read a run's
gates, quarantine and lineage. Every child id is bound to the path's workspace through a scoped loader."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import rows
from analystos.contracts.recipe import validate_recipe
from analystos.db.models import User
from analystos.governance.policy import require_role
from analystos.services import recipes as recipe_svc

router = APIRouter(prefix="/api", tags=["recipes"])


class RecipeIn(BaseModel):
    spec: dict[str, Any]


class RunIn(BaseModel):
    mode: Literal["preview", "materialize"] = "materialize"
    engine: Literal["auto", "sql", "duckdb"] | None = None
    limit: int = recipe_svc.PREVIEW_ROWS


@router.post("/workspaces/{workspace_id}/recipes/validate")
def validate(workspace_id: str, body: RecipeIn, user: User = Depends(current_user),
             session: Session = Depends(db, scope="function")):
    """The IR validator's verdict and the schema of every node; nothing is stored."""
    require_role(session, user, workspace_id, "analyst")
    v = validate_recipe(body.spec)
    return {"valid": True, "spec_hash": v.hash, "spec": v.stamped()}


@router.post("/workspaces/{workspace_id}/recipes")
def save(workspace_id: str, body: RecipeIn, user: User = Depends(current_user),
         session: Session = Depends(db, scope="function")):
    row = recipe_svc.save_recipe(session, user, workspace_id, body.spec)
    return recipe_svc.recipe_view(row)


@router.get("/workspaces/{workspace_id}/recipes")
def list_recipes(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return [recipe_svc.recipe_view(r) for r in recipe_svc.list_recipes(session, user, workspace_id)]


@router.get("/workspaces/{workspace_id}/recipes/{recipe_id}")
def get_recipe(workspace_id: str, recipe_id: str, user: User = Depends(current_user),
               session: Session = Depends(db, scope="function")):
    return recipe_svc.recipe_view(recipe_svc.get_recipe(session, user, recipe_id, workspace_id))


@router.post("/workspaces/{workspace_id}/recipes/{recipe_id}/publish")
def publish(workspace_id: str, recipe_id: str, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    return recipe_svc.recipe_view(recipe_svc.publish_recipe(session, user, recipe_id, workspace_id))


@router.get("/workspaces/{workspace_id}/recipes/{recipe_id}/compiled")
def compiled(workspace_id: str, recipe_id: str, target: Literal["sql", "duckdb", "dbt"] = "sql", dialect: str | None = None,
             user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return recipe_svc.compile_recipe(session, user, recipe_id, workspace_id, target=target, dialect=dialect)


@router.post("/workspaces/{workspace_id}/recipes/{recipe_id}/runs")
def run(workspace_id: str, recipe_id: str, body: RunIn, user: User = Depends(current_user)):
    """Preview (rows and gate results, nothing written) or materialize (outputs, quarantine, lineage)."""
    engine = None if body.engine in (None, "auto") else body.engine
    return recipe_svc.run_recipe(user, recipe_id, workspace_id, mode=body.mode, engine=engine,
                                 limit=max(1, min(body.limit, 1000)))


@router.get("/workspaces/{workspace_id}/recipe-runs")
def list_runs(workspace_id: str, recipe: str | None = None, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    return rows(recipe_svc.recipe_runs(session, user, workspace_id, recipe), exclude={"openlineage", "lineage"})


@router.get("/workspaces/{workspace_id}/recipe-runs/{run_id}")
def get_run(workspace_id: str, run_id: str, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    return recipe_svc.run_view(recipe_svc.get_run(session, user, run_id, workspace_id))


@router.get("/workspaces/{workspace_id}/recipe-runs/{run_id}/quarantine")
def quarantine(workspace_id: str, run_id: str, output: str, limit: int = 200, user: User = Depends(current_user)):
    """The rows a run quarantined for `output`, read through the query gateway."""
    return recipe_svc.quarantine_rows(user, run_id, workspace_id, output=output, limit=limit)


class SqlLineageIn(BaseModel):
    sql: str
    dialect: str = "postgres"


@router.post("/workspaces/{workspace_id}/lineage/sql")
def sql_lineage(workspace_id: str, body: SqlLineageIn, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    """Column lineage of a statement (view, INSERT ... SELECT, query) resolved against the columns of the
    assets this caller may read (P6-07). Nothing runs and nothing is stored; the SQL comes back redacted."""
    from dataclasses import asdict

    from analystos.evidence.lineage.sql import parse_lineage
    from analystos.governance.policy import resolve_scope

    scope = resolve_scope(session, user, workspace_id, minimum_role="analyst")
    catalog = {asset: [c for c in cols if f"{asset}.{c}" not in scope.denied_columns]
               for asset, cols in scope.columns.items()}
    result = parse_lineage(body.sql, body.dialect, catalog=catalog or None)
    return {"edges": [asdict(e) for e in result.edges], "confidence": result.confidence, "dialect": result.dialect,
            "sql_hash": result.sql_hash, "redacted_sql": result.redacted_sql, "target_table": result.target_table,
            "errors": result.errors}


@router.get("/workspaces/{workspace_id}/recipe-runs/{run_id}/lineage")
def lineage(workspace_id: str, run_id: str, user: User = Depends(current_user),
            session: Session = Depends(db, scope="function")):
    """Executed column lineage from the IR and the run's OpenLineage events (SQL literals redacted)."""
    row = recipe_svc.get_run(session, user, run_id, workspace_id)
    return {"lineage": row.lineage, "openlineage": row.openlineage}
