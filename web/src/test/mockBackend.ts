/**
 * A deterministic in-memory stand-in for the AnalystOS API, shared by the vitest accessibility
 * suite (fetch spy) and the Playwright journeys (route interception). Fixtures are typed against
 * the client's response shapes so a shape change breaks the typecheck, not just a screenshot.
 */
import type {
  AgentSpec, Alert, Approval, Artifact, AskResponse, CatalogAsset, ConsoleData, Insight, InsightDetail, Monitor, ModelsView,
  PlatformSettings, Run, RunDetail, Schedule, SkillSpec, Source, SourceKindInfo, TokenSavings, ToolSpec, Usage, User, WorkspaceDetail,
} from "../api";

export const WS = "ws_demo";
export const RUN = "run_demo";
export const INSIGHT = "ins_demo";
const T = "2026-09-25T09:00:00Z";

export const USER: User = {
  id: "usr_admin", email: "admin@analystos.local", name: "Ada Admin", is_admin: true, active: true, attributes: {}, created_at: T,
};

export const WORKSPACE: WorkspaceDetail = {
  id: WS, name: "IT Service Management", description: "Incident and change analytics", objective: "Why are P1 resolution times rising?",
  autonomy_level: 3, status: "active", settings: {}, policy_version: 2, created_by: USER.id, created_at: T, updated_at: T,
  counts: { query: 12, dataset: 2, metric: 3, chart: 4, dashboard: 1, runs: 1, verified_insights: 1, sources: 1 },
  role: "owner",
  policy: { max_rows: 5000, pii_access: "none", publish_destinations: ["superset"], publish_requires_approval: true, run_cost_budget_usd: 2,
    run_token_budget: 200000, restricted_columns: [] },
  members: [{ user_id: USER.id, role: "owner", email: USER.email, name: USER.name }],
};

const SOURCE: Source = {
  id: "src_sn", workspace_id: WS, kind: "servicenow", name: "ServiceNow", config: {}, secret_ref: "env:SN_PASSWORD", status: "ready",
  execution_mode: "staged", staging_schema: "stg_sn", last_discovered_at: T, last_error: null, created_at: T,
};

const INSIGHT_ROW: Insight = {
  id: INSIGHT, workspace_id: WS, run_id: RUN, hypothesis_id: "hyp_1", code: "F1", title: "Network group drives P1 breaches",
  finding: "Network resolves P1 incidents 2.1x slower than the median group.", confidence: 0.86, population_size: 4210,
  business_impact: {}, caveats: ["Q3 only"], evidence: [], verified: true, verification: { verified: true }, status: "verified",
  narrative_source: "rule", created_at: T,
};

export const RUN_ROW: Run = {
  id: RUN, workspace_id: WS, objective: "Why are P1 resolution times rising?", status: "COMPLETED", autonomy_level: 3, plan_version: 1,
  plan_hash: "a1b2c3d4e5f6", policy_version: 2, instructions: [], constraints: {}, control: "run", requested_by: USER.id,
  workflow_id: null, iteration: 1, tokens: 18250, cost_usd: 0.0421, error: null,
  summary: { summary_markdown: "**One verified finding.** Network drives P1 breaches.", verified_insights: 1, published: false },
  origin: { type: "user" }, created_at: T, started_at: T, finished_at: "2026-09-25T09:04:00Z",
};

const APPROVAL: Approval = {
  id: "apr_1", workspace_id: WS, run_id: RUN, action: "publish_dashboard", risk_tier: "medium", destination: "superset",
  affected_assets: ["incident"], payload_hash: "0123456789abcdef0123", plan_hash: "a1b2c3d4e5f6", policy_version: 2, requested_by: USER.id,
  status: "pending", decided_by: null, decided_at: null, reason: null, expires_at: "2099-01-01T00:00:00Z",
  evidence: { governance_review: { ok: true, problems: [] } }, created_at: T,
};

const RUN_DETAIL: RunDetail = {
  ...RUN_ROW, scope: {}, tasks: [
    { id: "tsk_1", run_id: RUN, key: "profile", agent_id: "profiler", title: "Profile incident data", status: "COMPLETED", depends_on: [],
      input: {}, output: {}, attempts: 1, plan_version: 1, seq: 1, error: null, started_at: T, finished_at: T },
  ],
  hypotheses: [{
    id: "hyp_1", workspace_id: WS, run_id: RUN, code: "H1", question: "Which group is slowest?", statement: "Network resolves P1s slower",
    spec: {}, priority: "high", priority_score: 0.9, status: "supported", methods: ["mann_whitney"], evidence: [], confidence: 0.86,
    conclusion: "Supported", parent_id: null, iteration: 1, origin: "planner", created_at: T,
    result: { test: "mann_whitney", n: 4210, p_value: 0.0004, p_adjusted: 0.001, effect_size: 0.41, effect_label: "medium" }, experiment_id: null,
  }],
  insights: [INSIGHT_ROW],
  approvals: [APPROVAL],
};

const INSIGHT_DETAIL: InsightDetail = { ...INSIGHT_ROW, queries: [], experiments: [], lineage: { nodes: [], edges: [] } };

const ARTIFACTS: Artifact[] = [{
  id: "art_dash", workspace_id: WS, run_id: RUN, type: "dashboard", name: "P1 resolution", version: 1, status: "draft", platform: null,
  external_id: null, external_url: null, creator_agent: "bi", creator_user: null, content: { charts: [], layout: [] }, content_hash: "ff00",
  created_at: T, updated_at: T,
}];

const CATALOG: CatalogAsset[] = [{
  id: "ast_inc", fq: "servicenow.incident", source_id: SOURCE.id, name: "incident", business_name: "Incidents",
  description: "One row per incident.", description_origin: "rule", reviewed: true, selected: true, lifecycle: "active", row_count: 4210,
  role: "fact", domain: "itsm", grain: "incident", confidence: 0.9, last_crawled_at: T,
  columns: [{ name: "priority", data_type: "TEXT", business_name: "Priority", description: null, tags: [], tags_origin: "crawler",
    role: "dimension", unit: null, pii: null, glossary: null }],
}];

const KINDS: SourceKindInfo[] = [{
  kind: "servicenow", label: "ServiceNow", category: "api", required: ["username"], optional: ["instance_url"], default_port: null, docs: "",
  secret_field: "password", execution_mode: "staged", dialect: "postgres", driver_installed: true, install_hint: null, enabled: true,
}];

export const ASK: AskResponse = {
  sql: "SELECT assignment_group, COUNT(*) AS p1 FROM stg_sn.incident WHERE priority = '1' GROUP BY 1 ORDER BY 2 DESC",
  explanation: "P1 incidents by assignment group.", chart: { type: "bar", x: "assignment_group", y: "p1" }, model: "rule", attempts: [],
  result: { query_id: "qry_1", columns: ["assignment_group", "p1"], rows: [["Network", 182], ["Desktop", 110]], row_count: 2, truncated: false },
};

const SETTINGS: PlatformSettings = {
  llm: {
    purpose_modes: { planning: "auto" }, routing_overrides: {}, profile_models: {}, disabled_models: [], cache_enabled: true, cache_ttl_hours: 168,
    cacheable_purposes: ["planning"], max_prompt_tokens: 16000, downgrade_below_budget_fraction: 0.25, compact_prompts: true,
    catalog_max_columns_per_table: 30, catalog_max_tables: 12,
  },
  analysis: { min_sample_size: 100 }, crawl: { default_mode: "incremental" }, monitors: { default_z_threshold: 3 },
  sources: { enabled_kinds: [], allow_pushdown: true, staged_max_rows: 1000000 }, features: { reports: true },
};

const MODELS: ModelsView = {
  allowlist: ["openrouter/auto"], profiles: { chat: { models: ["openrouter/auto"], provider: "openrouter", temperature: 0, max_tokens: 2000,
    timeout_seconds: 60, exclude_families: [] } },
  routing: { planning: "chat" }, providers: { openrouter: { kind: "chat", base_url: "https://openrouter.ai/api/v1" } }, available: { planning: true },
  effective: { planning: { profile: "chat", models: ["openrouter/auto"], mode: "auto", available: true, deterministic_path: true, decision_model: false } },
};

const MONITOR: Monitor = {
  id: "mon_1", workspace_id: WS, name: "P1 MTTR", kind: "metric_threshold", config: { metric: "mttr_hours", op: ">", value: 8 }, enabled: true,
  auto_investigate: false, state: "ok", last_evaluated_at: T, last_result: {}, created_by: USER.id, created_at: T,
};

const ALERT: Alert = {
  id: "alr_1", workspace_id: WS, monitor_id: MONITOR.id, severity: "warning", title: "P1 MTTR above 8h", message: "MTTR 9.4h > 8h",
  data: {}, dedupe_key: "k", status: "open", investigation_run_id: null, acknowledged_by: null, created_at: T, resolved_at: null,
};

const SCHEDULE: Schedule = {
  id: "sch_1", workspace_id: WS, name: "Weekly re-analysis", kind: "reanalysis", cron: "0 7 * * 1", timezone: "UTC", config: {},
  enabled: true, owner_id: USER.id, next_run_at: "2026-09-28T07:00:00Z", last_run_at: null, created_at: T, recent_runs: [],
};

const CONSOLE: ConsoleData = { messages: [], tool_calls: [], model_calls: [], queries: [],
  cost: { usd: 0.0421, tokens: 18250, model_calls: 3, jev_calls: 1, failed_calls: 0 } };

const AGENTS: AgentSpec[] = [{ id: "planner", version: "1.0", name: "Planner", description: "Plans analyses", capabilities: [], skills: [],
  tools: [], model_profile: "chat", phase: "plan", verification_required: false, enabled: true }];
const TOOLS: ToolSpec[] = [{ tool_id: "sql.query", name: "SQL query", version: "1.0", description: "Governed SQL", category: "data", risk: "low",
  approval_policy: "none", side_effects: "none", runtime: "python", min_role: "analyst", enabled: true }];
const SKILLS: SkillSpec[] = [{ id: "stats.mann_whitney", category: "statistics", description: "Two-sample test", tools: [], deterministic: true, enabled: true }];

const SAVINGS: TokenSavings = {
  days: 30,
  totals: { calls: 12, tokens_used: 40000, tokens_saved: 26000, cost_usd: 0.12, cache_hits: 4, deterministic_skips: 9, refused: 0, saved_share: 0.39 },
  by_purpose: { planning: { calls: 12, tokens_used: 40000, tokens_saved: 26000, cost_usd: 0.12, by_status: { ok: 3, cache_hit: 4, skipped: 9 } } },
};
const USAGE: Usage = { models: [], queries: [] };

export interface MockResponse {
  status: number;
  body: string;
  contentType: string;
}

const json = (body: unknown, status = 200): MockResponse => ({ status, body: JSON.stringify(body), contentType: "application/json" });

/** Route one request. `path` is the URL path after the host, with its query string. */
export function mockBackend(method: string, path: string, requestBody?: string | null): MockResponse {
  const url = new URL(path, "http://mock.local");
  const p = url.pathname.replace(/^\/api/, "");
  const m = method.toUpperCase();
  const W = `/workspaces/${WS}`;

  if (m === "POST" && p === "/auth/login") {
    const body = requestBody ? JSON.parse(requestBody) as { password?: string } : {};
    if (body.password !== "ChangeMe123!") return json({ error: { code: "unauthorized", message: "invalid credentials", details: {} } }, 401);
    return json({ access_token: "mock-token", token_type: "bearer", user: USER });
  }
  if (p.endsWith("/events")) {
    return { status: 200, body: `event: end\ndata: {"status":"COMPLETED"}\n\n`, contentType: "text/event-stream" };
  }
  const routes: [string, string, unknown][] = [
    ["GET", "/auth/me", USER],
    ["GET", "/workspaces", [WORKSPACE]],
    ["GET", W, WORKSPACE],
    ["GET", `${W}/sources`, [SOURCE]],
    ["GET", `${W}/analysis`, [RUN_ROW]],
    ["GET", `${W}/analysis/${RUN}`, RUN_DETAIL],
    ["GET", `${W}/analysis/${RUN}/console`, CONSOLE],
    ["GET", `${W}/insights`, [INSIGHT_ROW]],
    ["GET", `/insights/${INSIGHT}`, INSIGHT_DETAIL],
    ["GET", `${W}/artifacts`, ARTIFACTS],
    ["GET", `${W}/approvals`, [APPROVAL]],
    ["GET", `${W}/alerts`, [ALERT]],
    ["GET", `${W}/monitors`, [MONITOR]],
    ["GET", `${W}/schedules`, [SCHEDULE]],
    ["GET", `${W}/catalog`, CATALOG],
    ["GET", `${W}/crawls`, []],
    ["GET", `${W}/assets`, []],
    ["GET", `${W}/relationships`, []],
    ["GET", `${W}/audit`, []],
    ["GET", "/source-kinds", KINDS],
    ["GET", "/notifications", []],
    ["POST", `${W}/ask`, ASK],
    ["GET", "/agents", AGENTS],
    ["GET", "/tools", TOOLS],
    ["GET", "/skills", SKILLS],
    ["GET", "/admin/models", MODELS],
    ["GET", "/admin/usage", USAGE],
    ["GET", "/admin/audit", []],
    ["GET", "/admin/prompts", []],
    ["GET", "/admin/token-savings", SAVINGS],
    ["GET", "/admin/settings", { version: 3, settings: SETTINGS, defaults: SETTINGS, presets: { balanced: { planning: "always" } }, schema: {} }],
    ["GET", "/admin/settings/history", [{ version: 3, note: "initial", created_by: USER.id, created_at: T }]],
  ];
  for (const [rm, rp, body] of routes) if (rm === m && rp === p) return json(body);
  const notFound = json({ error: { code: "not_found", message: `mock: no route for ${m} ${p}`, details: {} } }, 404);
  // Unknown records are 404s, as on the server; other unlisted collection reads are empty lists.
  const foreignWorkspace = p.startsWith("/workspaces/") && !p.startsWith(W);
  const recordRead = /\/(ws|run|ins|art|sch|mon|alr|apr|src|crl|tsk)_[^/]*$/.test(p);
  if (m === "GET" && !foreignWorkspace && !recordRead) return json([]);
  return notFound;
}
