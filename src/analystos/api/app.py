from __future__ import annotations

import gc
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from analystos.api.routers import admin, analysis, artifacts, auth, capabilities, catalog, continuous, workspaces
from analystos.api.routers import decisions as decisions_router
from analystos.api.routers import mcp as mcp_router
from analystos.api.routers import registries as registries_router
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError
from analystos.core.logging import configure_logging, get_logger
from analystos.mcp import server as mcp_server

configure_logging()
log = get_logger("analystos.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The import graph (statistics, ML, reporting stacks) is hundreds of thousands of long-lived
    # objects. Freezing them keeps full GC collections from rescanning them: each one otherwise
    # pauses the event loop for 100-150 ms, which every open SSE stream feels (P4-C04 load test).
    gc.collect()
    gc.freeze()
    yield


app = FastAPI(title="Context2AI AnalystOS", version="0.1.0", lifespan=lifespan,
              description="Autonomous, governed data & analytics agent operating system (Phase 1 MVP).")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
for r in (auth.router, workspaces.router, analysis.router, artifacts.router, admin.router, continuous.router, catalog.router,
          capabilities.router, registries_router.router):
    app.include_router(r)
app.include_router(mcp_router.router)
app.include_router(decisions_router.router)
mcp_server.mount(app)  # MCP protocol endpoint at /mcp (P4-X06)


@app.exception_handler(AnalystOSError)
async def domain_error(_: Request, exc: AnalystOSError):
    return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_input", "message": "request validation failed",
                                                            "details": {"errors": exc.errors()}, "retryable": False}})


@app.get("/api/health")
def health():
    """Dependency health. Reports what is reachable; never claims readiness it cannot observe."""
    from sqlalchemy import text

    from analystos.db.base import get_engine

    checks: dict[str, dict] = {}

    def check(name, fn):
        started = time.perf_counter()
        try:
            detail = fn()
            checks[name] = {"ok": True, "ms": round((time.perf_counter() - started) * 1000), **(detail or {})}
        except Exception as exc:
            checks[name] = {"ok": False, "error": str(exc)[:200]}

    settings = get_settings()

    def pg():
        with get_engine().connect() as c:
            c.execute(text("select 1"))

    def redis_():
        import redis

        redis.Redis.from_url(settings.redis_url, socket_timeout=1).ping()

    def neo():
        if not settings.graph_enabled:  # optional projection; lineage and neighbourhood come from Postgres
            return {"enabled": False, "status": "disabled"}
        from analystos.graph.projection import _driver

        _driver().verify_connectivity()
        return {"enabled": True}

    def temporal():
        import socket

        host, port = settings.temporal_address.split(":")
        socket.create_connection((host, int(port)), timeout=1).close()

    def superset():
        import httpx

        httpx.get(f"{settings.superset_url}/health", timeout=2).raise_for_status()

    def models():
        from analystos.runtime.context import default_router

        r = default_router()
        return {"chat": r.available("planning"), "jev": r.available("risk_check")}

    for name, fn in (("postgres", pg), ("redis", redis_), ("neo4j", neo), ("temporal", temporal), ("superset", superset), ("models", models)):
        check(name, fn)
    return {"ok": checks["postgres"]["ok"], "orchestrator": settings.orchestrator, "checks": checks}
