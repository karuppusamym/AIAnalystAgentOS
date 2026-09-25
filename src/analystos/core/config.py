"""Runtime configuration. Every value comes from the environment (prefix ANALYSTOS_) or .env.

Secrets are never given defaults and are never logged. OPENROUTER_API_KEY is read unprefixed
because it is shared with the sibling projects and the OpenRouter tooling convention.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANALYSTOS_", env_file=".env", extra="ignore")

    env: str = "dev"
    # Control plane (application state). Never reachable from user/model SQL.
    database_url: str = "postgresql+psycopg://analystos:analystos@localhost:5432/analystos"
    # Analytics plane: staging database for API/file sources. Two identities:
    # the loader writes staged snapshots, the reader is the only identity the gateway uses.
    analytics_loader_url: str = "postgresql+psycopg://analystos_loader:loader@localhost:5432/analytics"
    analytics_reader_url: str = "postgresql+psycopg://analystos_reader:reader@localhost:5432/analytics"
    # Per-workspace NOLOGIN roles (<prefix><workspace id>) hold SELECT on that workspace's staged
    # schemas; the reader identity only reaches them via SET ROLE, so it cannot read across workspaces.
    analytics_workspace_role_prefix: str = "analystos_r_"
    redis_url: str = "redis://localhost:6379/0"
    # Neo4j is an optional projection of the Postgres lineage/relationship tables (spec v3 §8), off
    # by default: lineage and table neighbourhood are served from Postgres unless this is on.
    graph_enabled: bool = False
    budget_counter_prefix: str = "aos:budget:"  # run/workspace/purpose budget counters (P4-T07)
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "analystos-neo4j"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    # Task queues are `<prefix>-<workload>` (analysis, compute, publish, crawl, elt); see
    # config/task_queues.yaml. `analystos worker` serves `worker_queues` unless --queues is given.
    temporal_queue_prefix: str = "analystos"
    worker_queues: str = "all"
    # temporal | local. local runs the same durable steps in a background thread (tests, laptops).
    orchestrator: str = "temporal"

    superset_url: str = "http://localhost:8088"
    # Browser-facing base URL. In Docker, workers use ``superset_url`` over the
    # Compose network while people open this URL from their host browser.
    superset_public_url: str | None = None
    superset_username: str = "admin"
    superset_password: str = "admin"
    # How Superset reaches the analytics DB (from inside the compose network).
    superset_analytics_sqlalchemy_uri: str = "postgresql+psycopg2://analystos_reader:reader@postgres:5432/analytics"

    context2ai_url: str | None = None  # existing Context2AI service; local context store when unset
    jwt_secret: str = Field(default="dev-only-change-me-please-32bytes!!")
    jwt_ttl_minutes: int = 12 * 60
    bootstrap_admin_email: str = "admin@analystos.local"
    bootstrap_admin_password: str = "ChangeMe123!"

    models_config: Path = REPO_ROOT / "config" / "models.yaml"
    agents_dir: Path = REPO_ROOT / "config" / "agents"
    artifact_dir: Path = REPO_ROOT / "var" / "artifacts"
    upload_dir: Path = REPO_ROOT / "var" / "uploads"  # CSV/Parquet sources may only read below this directory

    # Gateway defaults (workspace policy may tighten, never loosen beyond these ceilings).
    query_timeout_seconds: int = 30
    query_max_rows: int = 50_000
    query_cache_ttl_seconds: int = 3600

    sandbox_timeout_seconds: int = 60
    sandbox_memory_mb: int = 1024

    servicenow_mock_url: str = "http://localhost:8090"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def openrouter_api_key() -> str | None:
    import os

    value = os.getenv("OPENROUTER_API_KEY", "").strip()
    return value or None
