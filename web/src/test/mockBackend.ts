/**
 * A deterministic in-memory stand-in for the AnalystOS API, shared by the vitest accessibility
 * suite (fetch spy) and the Playwright journeys (route interception). Fixtures are typed against
 * the client's response shapes so a shape change breaks the typecheck, not just a screenshot.
 */
import type {
  AgentSpec, Alert, Approval, Artifact, AskResponse, CapabilityManifest, CapabilitySummary, CatalogAsset, ConsoleData, Hypothesis, Insight,
  InsightDetail, Monitor, ModelsView, PlatformSettings, Run, RunDetail, Schedule, SkillSpec, Source, SourceKindInfo, TokenSavings, ToolSpec,
  Usage, User, WorkspaceDetail,
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
  business_impact: {}, caveats: ["Q3 only", "Data quality: 2.1% of resolved_at values are null"], evidence: [{ type: "query", id: "qry_h1" }],
  verified: true, status: "verified", narrative_source: "rule", created_at: T,
  verification: {
    verified: true,
    evaluate: [
      { check: "method_fit", passed: true, detail: "mann_whitney for numeric_by_segment; assumptions: independent samples" },
      { check: "sample_size", passed: true, detail: "n=4210, smallest group=182 (min 30)" },
      { check: "significance_after_bh", passed: true, detail: "q=0.001 alpha=0.05" },
      { check: "effect_size", passed: true, detail: "rank_biserial=0.41" },
      { check: "representative_population", passed: true, method: "time_window", detail: "time_window: 4,210 of 4,210 rows; not truncated" },
      { check: "no_overreach", passed: true, detail: "associational wording" },
      { check: "reproducible_rerun", passed: true, detail: "qry_h1: identical result hash" },
      { check: "second_method", passed: true, detail: "welch_t: p=0.0007 effect=0.38 agrees=true" },
    ],
    verify: { reproducible: true, second_method: { test: "welch_t", p_value: 0.0007, effect_size: 0.38 }, jev: { p_supports: 0.91, model: "typesafe/jev-1" },
      independent_model: { unavailable: "no model of another family allowed" }, contradictions: [] },
  },
};

/** An unverified finding on another hypothesis: its checks are not recorded, so trust reads as unknown. */
const INSIGHT_DRAFT: Insight = {
  ...INSIGHT_ROW, id: "ins_draft", hypothesis_id: "hyp_5", code: "F2", title: "Weekend P1s may close faster", verified: false,
  status: "draft", confidence: 0.4, population_size: 0, caveats: [], evidence: [], verification: {},
  finding: "Weekend P1 incidents may resolve faster; the evidence is thin.",
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
  evidence: { governance_review: { ok: true, problems: [] }, jev_consequential: 0.62 }, created_at: T,
  payload: { dashboards: [{ key: "p1_resolution", title: "P1 resolution (weekly)", charts: ["mttr_by_group", "breaches"] }],
    charts: [{ key: "mttr_by_group", type: "bar" }, { key: "breaches", type: "line" }] },
};

/** The last publication approved to the same destination: the inbox diffs the pending payload against it. */
const APPROVAL_EXECUTED: Approval = {
  ...APPROVAL, id: "apr_0", status: "executed", payload_hash: "fedcba9876543210fedc", policy_version: 1, decided_by: USER.id,
  decided_at: "2026-09-18T09:30:00Z", created_at: "2026-09-18T09:00:00Z",
  payload: { dashboards: [{ key: "p1_resolution", title: "P1 resolution", charts: ["mttr_by_group"] }], charts: [{ key: "mttr_by_group", type: "bar" }] },
};

const hyp = (id: string, code: string, status: string, statement: string, extra: Partial<Hypothesis> = {}): Hypothesis => ({
  id, workspace_id: WS, run_id: RUN, code, question: "Which group is slowest?", statement, spec: {}, priority: "medium", priority_score: 0.5,
  status, methods: ["mann_whitney"], evidence: [], confidence: null, conclusion: null, parent_id: null, iteration: 1, origin: "planner",
  created_at: T, result: null, experiment_id: null, ...extra,
});

const RUN_DETAIL: RunDetail = {
  ...RUN_ROW, scope: {}, tasks: [
    { id: "tsk_1", run_id: RUN, key: "profile", agent_id: "profiler", title: "Profile incident data", status: "COMPLETED", depends_on: [],
      input: {}, output: {}, attempts: 1, plan_version: 1, seq: 1, error: null, started_at: T, finished_at: T },
  ],
  hypotheses: [
    hyp("hyp_1", "H1", "supported", "Network resolves P1s slower", { priority: "high", priority_score: 0.9, confidence: 0.86, conclusion: "Supported",
      result: { test: "mann_whitney", n: 4210, p_value: 0.0004, p_adjusted: 0.001, effect_size: 0.41, effect_label: "rank_biserial" } }),
    hyp("hyp_2", "H2", "rejected", "P1 volume rose in Q3", { result: { test: "trend", n: 26, p_value: 0.4, p_adjusted: 0.6, effect_size: 0.02 } }),
    hyp("hyp_3", "H3", "testing", "Reassignments lengthen P1 resolution"),
    hyp("hyp_4", "H4", "proposed", "Change freezes delay fixes", { priority: "low", priority_score: 0.2 }),
    hyp("hyp_5", "H5", "inconclusive", "Weekend P1s close faster"),
    hyp("hyp_0", "H0", "superseded", "An earlier framing replaced by a replan"),
  ],
  insights: [INSIGHT_ROW, INSIGHT_DRAFT],
  approvals: [APPROVAL],
  instructions: [{ kind: "redirect", text: "Exclude auto-closed tickets.", at: T, feedback_id: "fb_1" }],
  constraints: { filters: [{ asset: "incident", column: "close_code", op: "!=", value: "auto" }], focus: [] },
};

const INSIGHT_DETAIL: InsightDetail = { ...INSIGHT_ROW, queries: [{
  id: "qry_h1", workspace_id: WS, source_id: "src_sn", run_id: RUN, task_id: null, actor: "agent:data_scientist", purpose: "hypothesis.test",
  sql: "SELECT 1", executed_sql: "SELECT 1", fingerprint: "fp", status: "ok", rejected_reason: null, referenced_assets: ["incident"], row_count: 2,
  truncated: false, columns: ["g", "n"], result_hash: "9f86d081884c7d659a2feaa0c55ad015", cache_hit: false, duration_ms: 42, created_at: T,
}], experiments: [], lineage: { nodes: [], edges: [] } };

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
  data: { severity: "warning", triage: { p_material: 0.64, model: "typesafe/jev-1" } }, dedupe_key: "k", status: "open", investigation_run_id: null, acknowledged_by: null, created_at: T, resolved_at: null,
};

const SCHEDULE: Schedule = {
  id: "sch_1", workspace_id: WS, name: "Weekly re-analysis", kind: "reanalysis", cron: "0 7 * * 1", timezone: "UTC", config: {},
  enabled: true, owner_id: USER.id, next_run_at: "2026-09-28T07:00:00Z", last_run_at: null, created_at: T, recent_runs: [],
};

const CONSOLE: ConsoleData = { messages: [], tool_calls: [], model_calls: [], queries: [],
  cost: { usd: 0.0421, tokens: 18250, model_calls: 3, jev_calls: 1, failed_calls: 0, cache_hits: 2, deterministic_skips: 5, tokens_saved: 9100 } };

// ------------------------------------------------------------------------------------ capability registry
const cap = (m: Partial<CapabilitySummary> & Pick<CapabilitySummary, "id" | "kind" | "summary">): CapabilitySummary => ({
  version: "1.0.0", ref: `${m.id}@${m.version ?? "1.0.0"}`, source: "builtin", entry: null, determinism: "deterministic", side_effect: "none",
  cost_class: "free", certification: { status: "certified", evidence: "tests/unit" }, autonomous_ok: true, needs_approval: false, tags: [],
  enabled: null, ...m,
});

/** A plugin method installed from a Python entry point: the registry must show and run it with no UI change. */
export const PLUGIN_METHOD: CapabilityManifest = {
  apiVersion: "analystos/v1", kind: "Method", id: "method.acme_funnel", version: "0.3.0",
  summary: "Funnel conversion by segment with a chi-square test", entry: "python:acme_methods.funnel:Funnel",
  input_schema: {
    type: "object", required: ["asset", "steps"],
    properties: {
      asset: { type: "string", title: "Asset", description: "Table holding one row per event" },
      steps: { type: "array", items: { type: "string" }, minItems: 2, description: "Funnel steps, in order" },
      segment: { anyOf: [{ type: "string" }, { type: "null" }], default: null, description: "Optional column to compare" },
      alpha: { type: "number", minimum: 0, maximum: 0.5, default: 0.05 },
      window_days: { type: "integer", minimum: 1, default: 30 },
      method: { enum: ["chi_square", "fisher"], default: "chi_square" },
      include_nulls: { type: "boolean", default: false },
    },
  },
  output_schema: { $ref: "schemas/stat_result.json" }, determinism: "deterministic", side_effect: "read_source", cost_class: "query",
  permissions: ["scope:read"], requires: ["engine:sql"], certification: { status: "tested", evidence: "acme_methods/tests/test_funnel.py" },
  ui: { form: "auto", renderer: "renderer.stat_result" }, tags: ["statistical", "plugin"], spec: {}, source: "entrypoint:acme-methods",
};

export const FUNNEL_RESULT = {
  test: "chi_square", n: 1830, p_value: 0.0021, p_adjusted: 0.0042, effect_size: 0.12, effect_label: "cramers_v", supported: true,
  groups: [{ segment: "web", conversion: 0.31, n: 1200 }, { segment: "mobile", conversion: 0.22, n: 630 }], warnings: [],
};

export const CAPABILITIES: CapabilitySummary[] = [
  cap({ id: "playbook.investigate", kind: "Playbook", summary: "The v1 investigation: context → hypotheses → tests → verified findings" }),
  cap({ id: "agent.data_scientist", kind: "Agent", summary: "Tests hypotheses with compiled analysis specs", determinism: "seeded" }),
  cap({ id: "method.trend", kind: "Method", summary: "Volume or numeric outcome over time", side_effect: "read_source", cost_class: "query" }),
  cap({ id: PLUGIN_METHOD.id, kind: "Method", version: PLUGIN_METHOD.version, summary: PLUGIN_METHOD.summary, source: PLUGIN_METHOD.source!,
    entry: PLUGIN_METHOD.entry!, side_effect: "read_source", cost_class: "query", certification: { status: "tested", evidence: null },
    autonomous_ok: false, tags: ["statistical", "plugin"] }),
  cap({ id: "tool.sql_query", kind: "Tool", summary: "Governed SQL through the query gateway", side_effect: "read_source", cost_class: "query" }),
  cap({ id: "publisher.superset", kind: "Publisher", summary: "Publishes dashboards to Apache Superset", side_effect: "write_external",
    needs_approval: true, certification: { status: "tested" }, autonomous_ok: false }),
  cap({ id: "connector.servicenow", kind: "Connector", summary: "ServiceNow table API", side_effect: "read_source" }),
];

function capabilityList(workspace: string | null) {
  const enabledIds = new Set(["playbook.investigate", "agent.data_scientist", "method.trend", "tool.sql_query", "connector.servicenow"]);
  return { digest: "d1e2f3a4b5c6d7e8", capabilities: CAPABILITIES.map((c) => ({ ...c, enabled: workspace ? enabledIds.has(c.id) : null })) };
}

function manifestFor(id: string): CapabilityManifest | null {
  if (id === PLUGIN_METHOD.id) return PLUGIN_METHOD;
  const c = CAPABILITIES.find((x) => x.id === id);
  if (!c) return null;
  return { apiVersion: "analystos/v1", kind: c.kind, id: c.id, version: c.version, summary: c.summary, entry: c.entry, input_schema: {},
    output_schema: {}, determinism: c.determinism, side_effect: c.side_effect, cost_class: c.cost_class, permissions: [], requires: [],
    certification: c.certification, ui: { form: "auto", renderer: null }, tags: c.tags, spec: {}, source: c.source };
}

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
  // capability registry: list (with enablement per workspace), manifest, enable toggle, reload, run
  if (m === "GET" && p === "/capabilities") return json(capabilityList(url.searchParams.get("workspace_id")));
  const capId = /^\/capabilities\/([^/]+)$/.exec(p);
  if (m === "GET" && capId) {
    const man = manifestFor(decodeURIComponent(capId[1]));
    return man ? json(man) : json({ error: { code: "not_found", message: "capability not found", details: {} } }, 404);
  }
  const wsCap = new RegExp(`^${W}/capabilities/([^/]+)(/invoke)?$`).exec(p);
  if (wsCap && m === "PUT" && !wsCap[2]) {
    const body = requestBody ? JSON.parse(requestBody) as { enabled?: boolean } : {};
    return json({ capability_id: decodeURIComponent(wsCap[1]), enabled: !!body.enabled });
  }
  if (wsCap && m === "POST" && wsCap[2]) {
    return decodeURIComponent(wsCap[1]) === PLUGIN_METHOD.id
      ? json({ status: "ok", capability: PLUGIN_METHOD.id, side_effect: "read_source", result: FUNNEL_RESULT })
      : json({ error: { code: "not_found", message: "no invoke route", details: {} } }, 404);
  }
  if (m === "POST" && p === "/admin/capabilities/reload") {
    return json({ digest: "d1e2f3a4b5c6d7e8", previous_digest: "d1e2f3a4b5c6d7e8", count: CAPABILITIES.length, problems: [] });
  }
  if (m === "GET" && p === `${W}/mcp/capabilities`) return json([]);
  // feedback: the server has no "accept" kind yet (services/runs.py FEEDBACK_KINDS), so it is refused as there
  if (m === "POST" && p === `${W}/analysis/${RUN}/feedback`) {
    const body = requestBody ? JSON.parse(requestBody) as { kind?: string | null } : {};
    if (body.kind === "accept") {
      return json({ error: { code: "invalid_input", message: "kind must be one of ['add_context', 'deeper_analysis', 'question', 'redirect', 'reject_finding']",
        details: {} } }, 400);
    }
    return json({ kind: body.kind || "redirect", classified_by: body.kind ? "user" : "jev:typesafe/jev-1", consequential_p: 0.04,
      interpretation: body.kind === "reject_finding" ? undefined : { filters: [], focus: ["Network"], summary: "Focus on the Network group.", interpreted_by: "rule" },
      replan: { plan_version: 2, tasks_reset: [], tasks_removed: [], approvals_invalidated: [] }, feedback_id: "fb_2" });
  }
  if (m === "GET" && p === `${W}/approvals`) {
    const status = url.searchParams.get("status");
    return json([APPROVAL, APPROVAL_EXECUTED].filter((a) => !status || a.status === status));
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
    ["GET", `${W}/insights`, [INSIGHT_ROW, INSIGHT_DRAFT]],
    ["GET", `/insights/${INSIGHT}`, INSIGHT_DETAIL],
    ["GET", `${W}/artifacts`, ARTIFACTS],
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
