"""Runtime configuration. Every value comes from the environment (prefix ANALYSTOS_) or .env.

Secrets are never given defaults and are never logged. OPENROUTER_API_KEY is read unprefixed
because it is shared with the sibling projects and the OpenRouter tooling convention.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    # BI logins (P4-02): one LOGIN role per workspace (<prefix><workspace id>) that Superset connects as;
    # a member of that workspace's reader role only, so BI SQL cannot SET ROLE into another workspace.
    # Its password is derived from analytics_bi_secret (falls back to jwt_secret), never stored.
    analytics_bi_role_prefix: str = "analystos_bi_"
    analytics_bi_secret: str | None = None
    # Write path (P4-E06, spec v3 §7.4): a third analytics identity used only by the BuildGateway. It
    # reaches per-workspace NOLOGIN build roles (<prefix><workspace id>) by SET ROLE; each holds CREATE
    # on that workspace's designated target schemas only and reads its staged schemas through the
    # workspace reader role. Never used for queries; the query identities never write.
    analytics_builder_url: str = "postgresql+psycopg://analystos_builder:builder@localhost:5432/analytics"
    analytics_build_role_prefix: str = "analystos_b_"
    # The customer's dbt runner (P4-E04): a dbt Core executable run as a separate process (its own
    # venv or container image), never imported in-process. Projects and job logs live under build_dir.
    dbt_executable: str = "dbt"
    build_dir: Path = REPO_ROOT / "var" / "builds"
    build_timeout_seconds: int = 1800
    # Connection pools per plane (P4-S05; db/pools.py). Per process: control <= size + overflow;
    # analytics and loader the same per URL. `none` = no client-side pool (behind PgBouncer).
    # analytics_/loader_pool_mode inherit db_pool_mode when unset.
    db_pool_mode: Literal["queue", "none"] = "queue"
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout: float = Field(default=30.0, gt=0)
    db_pool_recycle: int = -1  # seconds; -1 = never (set below the pooler's/firewall's idle cut-off)
    analytics_pool_mode: Literal["queue", "none"] | None = None
    analytics_pool_size: int = Field(default=5, ge=1)
    analytics_max_overflow: int = Field(default=5, ge=0)
    analytics_pool_timeout: float = Field(default=30.0, gt=0)
    loader_pool_mode: Literal["queue", "none"] | None = None
    loader_pool_size: int = Field(default=2, ge=1)
    loader_max_overflow: int = Field(default=3, ge=0)
    loader_pool_timeout: float = Field(default=30.0, gt=0)
    # The control/analytics/loader URLs go through a transaction-mode pooler (PgBouncer): no
    # server-side prepared statements. The builder URL must stay direct or session-pooled.
    db_transaction_pooler: bool = False
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

    # Knowledge index embeddings (P4-K10): auto | hashing | sentence_transformers. `auto` uses the
    # local sentence-transformer when the `embeddings` extra and the model are installed, hashing
    # otherwise. The model is never downloaded at runtime unless allowed (air-gapped by default).
    # Context2AI is reached through context providers (knowledge/providers.py), not a URL here.
    knowledge_embedding_provider: str = "auto"
    knowledge_embedding_model: str = "BAAI/bge-small-en-v1.5"  # 384-d, ~130 MB; best of four on the K10 benchmark
    knowledge_embedding_dim: int | None = None  # None = the provider's native dimension (hashing: 256)
    knowledge_embedding_allow_download: bool = False
    jwt_secret: str = Field(default="dev-only-change-me-please-32bytes!!")
    jwt_ttl_minutes: int = 12 * 60
    bootstrap_admin_email: str = "admin@analystos.local"
    bootstrap_admin_password: str = "ChangeMe123!"

    models_config: Path = REPO_ROOT / "config" / "models.yaml"
    # Air-gapped install (P4-S04, spec v3 §8): only providers declared `egress: internal` in the models
    # config are routed to, the model transport refuses every other host, and the DecisionService runs
    # on `rules` / `local_classifier` only. Pair with ANALYSTOS_MODELS_CONFIG=config/models.airgapped.yaml.
    air_gapped: bool = False

    # OIDC single sign-on (SEC-001..003), alongside password login. Authorization-code flow with PKCE;
    # the client secret comes from the environment only. Group -> role mapping: `oidc_mapping_file`.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None  # confidential clients; PKCE alone for public clients
    oidc_redirect_uri: str = "http://localhost:8000/api/auth/oidc/callback"
    oidc_scopes: str = "openid email profile groups"
    oidc_groups_claim: str = "groups"
    oidc_mapping_file: Path = REPO_ROOT / "config" / "oidc.yaml"
    oidc_jwks_file: Path | None = None  # static JWKS (air-gapped IdP mirrors, tests); else the discovery jwks_uri
    oidc_discovery_url: str | None = None  # defaults to <issuer>/.well-known/openid-configuration
    oidc_provider_name: str = "Single sign-on"
    password_login: bool = True  # local accounts stay available (break-glass admin) unless turned off
    web_url: str = "http://localhost:5173"  # where the OIDC callback returns the browser
    agents_dir: Path = REPO_ROOT / "config" / "agents"
    artifact_dir: Path = REPO_ROOT / "var" / "artifacts"
    upload_dir: Path = REPO_ROOT / "var" / "uploads"  # CSV/Parquet sources may only read below this directory

    # Gateway defaults (workspace policy may tighten, never loosen beyond these ceilings).
    query_timeout_seconds: int = 30
    query_max_rows: int = 50_000
    query_cache_ttl_seconds: int = 3600

    sandbox_timeout_seconds: int = 60
    sandbox_memory_mb: int = 1024
    # Isolation of sandbox children (P4-02, sandbox/isolation.py): container = docker run per execution
    # (no network, read-only root, cgroup limits); process = namespaces + read-only root + rlimits in the
    # worker; auto = container when its image is present, else process; none available = refused.
    # off = rlimits only, development only (refused when env is production).
    sandbox_isolation: Literal["auto", "container", "process", "off"] = "auto"
    sandbox_container_image: str = "analystos-sandbox:latest"
    sandbox_container_python: str = "python3"
    sandbox_container_runtime: str | None = None  # e.g. runsc (gVisor) where installed
    sandbox_container_startup_seconds: float = 5.0  # added to the wall clock for `docker run` start-up
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = Field(default=64, ge=4)
    sandbox_scratch_mb: int = Field(default=64, ge=1)
    # Hidden from process-mode children (secrets on the worker's filesystem); /run/secrets and
    # /var/run/secrets (service-account tokens) are always masked.
    sandbox_masked_paths: list[str] = Field(default_factory=lambda: [
        "/root", "/etc/analystos", "/etc/ssl/private", str(REPO_ROOT / ".env"), str(REPO_ROOT / "var")])

    servicenow_mock_url: str = "http://localhost:8090"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # Outbound HTTP (P7-11, tools/http.py). HTTP tool capabilities may call only these hostnames
    # (comma-separated; empty = none). Every outbound call (HTTP tools and MCP servers) is refused when
    # the host resolves to a non-public address, unless the operator lists that hostname or network
    # (CIDR) here -- e.g. "127.0.0.1,mcp.internal,10.20.0.0/16" for internal MCP servers.
    http_tool_allowlist: str = ""
    outbound_private_hosts: str = ""
    # MCP servers only: loopback and RFC 1918 ranges are allowed by default (owner decision 2026-09-26) so
    # in-cluster servers keep working; link-local/metadata and CGNAT stay refused. Set "" to require listing.
    mcp_private_hosts: str = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    http_tool_timeout_seconds: float = 30.0
    http_tool_max_bytes: int = Field(default=1_000_000, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def openrouter_api_key() -> str | None:
    import os

    value = os.getenv("OPENROUTER_API_KEY", "").strip()
    return value or None
