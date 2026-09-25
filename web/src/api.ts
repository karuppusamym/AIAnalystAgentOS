/**
 * Typed client for the AnalystOS FastAPI backend (src/analystos/api/routers/*.py).
 *
 * Every call sends `Authorization: Bearer <token>`. Errors come back as
 * {"error": {code, message, details, retryable}} and are raised as ApiError.
 */
import { readSSE, type SSEMessage } from "./lib/sse";

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type Dict = Record<string, unknown>;

// ----------------------------------------------------------------------------------- models
export interface User {
  id: string;
  email: string;
  name: string;
  is_admin: boolean;
  active: boolean;
  attributes: Dict;
  created_at: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export interface WorkspaceCounts {
  query: number;
  dataset: number;
  metric: number;
  chart: number;
  dashboard: number;
  runs: number;
  verified_insights: number;
  sources: number;
}

export interface Workspace {
  id: string;
  name: string;
  description: string;
  objective: string;
  autonomy_level: number;
  status: string;
  settings: Dict;
  policy_version: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  counts?: WorkspaceCounts;
}

export interface Member {
  user_id: string;
  role: string;
  email: string;
  name: string;
}

export interface WorkspaceDetail extends Workspace {
  counts: WorkspaceCounts;
  role: string;
  policy: WorkspacePolicy;
  members: Member[];
}

export interface WorkspacePolicy {
  max_rows?: number;
  query_timeout_seconds?: number;
  max_queries_per_run?: number;
  run_token_budget?: number;
  run_cost_budget_usd?: number;
  workspace_monthly_cost_budget_usd?: number;
  allowed_models?: string[];
  allowed_providers?: string[];
  restricted_columns?: string[];
  pii_columns?: string[];
  pii_access?: "none" | "restricted" | "allowed";
  tool_denylist?: string[];
  publish_destinations?: string[];
  publish_requires_approval?: boolean;
  separation_of_duties?: boolean;
  approval_ttl_hours?: number;
  max_iterations?: number;
  alpha?: number;
  [k: string]: unknown;
}

export interface Source {
  id: string;
  workspace_id: string;
  kind: string;
  name: string;
  config: Dict;
  secret_ref: string | null;
  status: string;
  execution_mode: string;
  staging_schema: string | null;
  last_discovered_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface TopValue {
  value: unknown;
  count: number;
}

export interface ColumnProfile {
  null_rate?: number;
  distinct?: number;
  top_values?: TopValue[];
  min?: unknown;
  max?: unknown;
  mean?: number | null;
  references?: unknown;
  [k: string]: unknown;
}

export interface SourceColumn {
  id: number;
  asset_id: string;
  name: string;
  ordinal: number;
  data_type: string;
  semantic_type: string | null;
  nullable: boolean;
  is_key: boolean;
  business_name: string | null;
  description: string | null;
  tags: string[];
  profile: ColumnProfile;
}

export interface Asset {
  id: string;
  source_id: string;
  workspace_id: string;
  schema_name: string;
  name: string;
  source_name: string;
  kind: string;
  selected: boolean;
  row_count: number | null;
  freshness_at: string | null;
  business_name: string | null;
  description: string | null;
  stats: Dict;
  created_at: string;
  fq: string;
  columns: SourceColumn[];
}

export interface DiscoveredAsset {
  source_name: string;
  name: string;
  schema_name: string | null;
  kind: string;
  row_count: number | null;
  description: string | null;
  business_name: string | null;
  columns: { name: string; data_type: string }[];
}

export interface DiscoverResponse {
  assets: DiscoveredAsset[];
  test: Dict;
}

export interface Relationship {
  id: string;
  workspace_id: string;
  from_asset_id: string;
  from_column: string;
  to_asset_id: string;
  to_column: string;
  cardinality: string;
  confidence: number;
  validated: boolean;
  evidence: Dict;
  origin: string;
  from_asset: string | null;
  to_asset: string | null;
}

export interface RunTask {
  id: string;
  run_id: string;
  key: string;
  agent_id: string;
  title: string;
  status: string;
  depends_on: string[];
  input: Dict;
  output: Dict;
  attempts: number;
  plan_version: number;
  seq: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface HypothesisResult {
  test?: string | null;
  n?: number | null;
  p_value?: number | null;
  p_adjusted?: number | null;
  effect_size?: number | null;
  effect_label?: string | null;
  highlights?: Dict | null;
  groups?: Dict[] | null;
  warnings?: string[] | null;
}

export interface Hypothesis {
  id: string;
  workspace_id: string;
  run_id: string;
  code: string;
  question: string;
  statement: string;
  spec: Dict;
  priority: string;
  priority_score: number;
  status: string;
  methods: string[];
  evidence: string[];
  confidence: number | null;
  conclusion: string | null;
  parent_id: string | null;
  iteration: number;
  origin: string;
  created_at: string;
  result: HypothesisResult | null;
  experiment_id: string | null;
}

export interface EvidenceRef {
  type: string;
  id: string;
  label?: string;
}

export interface VerificationCheck {
  check: string;
  passed: boolean;
  detail: string;
}

export interface Verification {
  reason?: { claim?: string; question?: string; required_evidence?: string[] };
  evaluate?: VerificationCheck[];
  verify?: {
    reproducible?: boolean;
    second_method?: Dict | null;
    independent_model?: { model?: string; review?: Dict; unavailable?: string } | null;
    jev?: { p_supports: number; model: string } | null;
    contradictions?: string[];
  };
  verified?: boolean;
  note?: string;
}

export interface Insight {
  id: string;
  workspace_id: string;
  run_id: string;
  hypothesis_id: string | null;
  code: string;
  title: string;
  finding: string;
  confidence: number;
  population_size: number;
  business_impact: Dict;
  caveats: string[];
  evidence: EvidenceRef[];
  verified: boolean;
  verification: Verification;
  status: string;
  narrative_source: string;
  created_at: string;
}

export interface QueryExecution {
  id: string;
  workspace_id: string;
  source_id: string | null;
  run_id: string | null;
  task_id: string | null;
  actor: string;
  purpose: string;
  sql: string;
  executed_sql: string | null;
  fingerprint: string | null;
  status: string;
  rejected_reason: string | null;
  referenced_assets: string[];
  row_count: number;
  truncated: boolean;
  columns: string[];
  result_hash: string | null;
  result_preview?: unknown[];
  cache_hit: boolean;
  duration_ms: number;
  created_at: string;
}

export interface Experiment {
  id: string;
  hypothesis_id: string | null;
  run_id: string;
  method: string;
  params: Dict;
  result: Dict;
  query_ids: string[];
  role: string;
  created_at: string;
}

export interface LineageNode {
  type: string;
  id: string;
}

export interface LineageEdge {
  from: [string, string];
  relation: string;
  to: [string, string];
}

export interface Lineage {
  nodes: LineageNode[];
  edges: LineageEdge[];
}

export interface InsightDetail extends Insight {
  queries: QueryExecution[];
  experiments: Experiment[];
  lineage: Lineage;
}

export interface Approval {
  id: string;
  workspace_id: string;
  run_id: string | null;
  action: string;
  risk_tier: string;
  destination: string | null;
  affected_assets: string[];
  payload_hash: string;
  plan_hash: string | null;
  policy_version: number;
  requested_by: string;
  status: string;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  expires_at: string;
  evidence: Dict;
  created_at: string;
}

export interface Publication {
  status?: string;
  urls?: Record<string, string>;
  external_ids?: Dict;
  publication_id?: string;
  reused?: boolean;
}

export interface RunSummary {
  summary_markdown?: string;
  summary_source?: string;
  verified_insights?: number;
  published?: boolean;
  publication?: Publication | null;
  cancel_outcome?: string;
  [k: string]: unknown;
}

export interface Run {
  id: string;
  workspace_id: string;
  objective: string;
  status: string;
  autonomy_level: number;
  plan_version: number;
  plan_hash: string | null;
  policy_version: number;
  instructions: Dict[];
  constraints: Dict;
  control: string;
  requested_by: string;
  workflow_id: string | null;
  iteration: number;
  tokens: number;
  cost_usd: number;
  error: string | null;
  summary: RunSummary;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunDetail extends Run {
  scope: Dict;
  plan?: Dict;
  tasks: RunTask[];
  hypotheses: Hypothesis[];
  insights: Insight[];
  approvals: Approval[];
}

export interface RunEvent {
  id: number;
  type: string;
  payload: Dict;
  actor: string | null;
  created_at: string;
}

export interface FeedbackResponse {
  kind: string;
  classified_by: string;
  consequential_p: number | null;
  note?: string;
  interpretation?: { filters: Dict[]; focus: string[]; summary: string; interpreted_by: string };
  replan?: { plan_version: number; tasks_reset: unknown; tasks_removed: unknown; approvals_invalidated: unknown };
  feedback_id: string;
}

export interface AgentMessage {
  id: number;
  run_id: string;
  task_id: string | null;
  agent_id: string;
  kind: string;
  content: string;
  data: Dict;
  created_at: string;
}

export interface ToolExecution {
  id: number;
  run_id: string | null;
  task_id: string | null;
  agent_id: string | null;
  tool_id: string;
  status: string;
  input: Dict;
  output: Dict;
  decision: Dict;
  latency_ms: number;
  error: string | null;
  created_at: string;
}

export interface ModelCall {
  id: number;
  run_id: string | null;
  task_id: string | null;
  agent_id: string | null;
  purpose: string;
  profile: string;
  provider: string;
  model: string;
  prompt_version: string | null;
  status: string;
  attempt: number;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  error: string | null;
  created_at: string;
}

export interface ConsoleData {
  messages: AgentMessage[];
  tool_calls: ToolExecution[];
  model_calls: ModelCall[];
  queries: QueryExecution[];
  cost: { usd: number; tokens: number; model_calls: number; jev_calls: number; failed_calls: number };
}

export interface QueryResult {
  query_id: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  cache_hit?: boolean;
  fingerprint?: string;
  result_hash?: string;
  duration_ms?: number;
  referenced_assets?: string[];
  sql?: string;
}

export interface AskResponse {
  sql: string;
  explanation: string | null;
  chart: { type?: string; x?: string | null; y?: string | null } | null;
  model: string | null;
  attempts: { sql: string; error: string }[];
  result: QueryResult;
}

export interface Artifact {
  id: string;
  workspace_id: string;
  run_id: string | null;
  type: string;
  name: string;
  version: number;
  status: string;
  platform: string | null;
  external_id: string | null;
  external_url: string | null;
  creator_agent: string | null;
  creator_user: string | null;
  content: Dict;
  content_hash: string;
  created_at: string;
  updated_at: string;
}

export interface ArtifactVersion {
  id: number;
  artifact_id: string;
  version: number;
  content_hash: string;
  created_by: string | null;
  created_at: string;
}

export interface ArtifactDetail extends Artifact {
  versions: ArtifactVersion[];
  lineage: Lineage;
}

export interface AgentSpec {
  id: string;
  version: string;
  name: string;
  description: string;
  capabilities: string[];
  skills: string[];
  tools: string[];
  model_profile: string;
  phase: string;
  verification_required: boolean;
  enabled: boolean;
  [k: string]: unknown;
}

export interface ToolSpec {
  tool_id: string;
  name: string;
  version: string;
  description: string;
  category: string;
  risk: string;
  approval_policy: string;
  side_effects: string;
  runtime: string;
  min_role: string;
  enabled: boolean;
  [k: string]: unknown;
}

export interface SkillSpec {
  id: string;
  category: string;
  description: string;
  tools: string[];
  deterministic: boolean;
  enabled: boolean;
  [k: string]: unknown;
}

export interface ModelProfile {
  models: string[];
  provider: string;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  exclude_families: string[];
}

export interface ModelsView {
  allowlist: string[];
  profiles: Record<string, ModelProfile>;
  routing: Record<string, string>;
  providers: Record<string, { kind: string; base_url: string }>;
  available: Record<string, boolean>;
}

export interface Usage {
  models: { purpose: string; model: string; provider: string; calls: number; cost_usd: number; avg_latency_ms: number; failed: number }[];
  queries: { status: string; count: number; avg_ms: number }[];
}

export interface AuditEvent {
  id: number;
  workspace_id: string | null;
  run_id: string | null;
  actor: string;
  action: string;
  target: string | null;
  decision: string | null;
  reasons: string[];
  details: Dict;
  created_at: string;
}

// ----------------------------------------------------------------------------------- errors
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Dict;

  constructor(status: number, code: string, message: string, details: Dict = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/** Turn any thrown value into a message suitable for display. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const extra = err.code === "invalid_input" && Array.isArray((err.details as { errors?: unknown[] }).errors)
      ? ": " + ((err.details as { errors: { loc?: unknown[]; msg?: string }[] }).errors
        .map((e) => `${(e.loc ?? []).slice(1).join(".")} ${e.msg ?? ""}`.trim()).join("; "))
      : "";
    return `${err.message}${extra}`;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

// ----------------------------------------------------------------------------------- token store
const TOKEN_KEY = "analystos.token";
const USER_KEY = "analystos.user";

function storageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: keep the session in memory only */
  }
}

let token: string | null = storageGet(TOKEN_KEY);
let unauthorizedHandler: (() => void) | null = null;

export const session = {
  get token(): string | null {
    return token;
  },
  get user(): User | null {
    const raw = storageGet(USER_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as User;
    } catch {
      return null;
    }
  },
  set(t: string, user: User) {
    token = t;
    storageSet(TOKEN_KEY, t);
    storageSet(USER_KEY, JSON.stringify(user));
  },
  clear() {
    token = null;
    storageSet(TOKEN_KEY, null);
    storageSet(USER_KEY, null);
  },
  onUnauthorized(fn: (() => void) | null) {
    unauthorizedHandler = fn;
  },
};

// ----------------------------------------------------------------------------------- transport
export const API_BASE = "/api";

function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function parseError(resp: Response): Promise<ApiError> {
  let code = `http_${resp.status}`;
  let message = resp.statusText || `request failed (${resp.status})`;
  let details: Dict = {};
  try {
    const body = (await resp.json()) as { error?: { code?: string; message?: string; details?: Dict }; detail?: unknown };
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      details = body.error.details ?? {};
    } else if (body?.detail) {
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    }
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(resp.status, code, message, details);
}

export async function request<T>(method: string, path: string, body?: unknown, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...authHeaders() };
  let payload: BodyInit | undefined;
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, { method, headers, body: payload, ...init });
  } catch (err) {
    throw new ApiError(0, "network_error", `API unreachable: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!resp.ok) {
    const err = await parseError(resp);
    if (resp.status === 401 && !path.startsWith("/auth/login")) unauthorizedHandler?.();
    throw err;
  }
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const get = <T>(p: string) => request<T>("GET", p);
const post = <T>(p: string, b?: unknown) => request<T>("POST", p, b ?? {});
const put = <T>(p: string, b: unknown) => request<T>("PUT", p, b);
const patch = <T>(p: string, b: unknown) => request<T>("PATCH", p, b);
const del = <T>(p: string) => request<T>("DELETE", p);
const e = encodeURIComponent;

function qs(params: Record<string, string | number | undefined | null>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${e(k)}=${e(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

// ----------------------------------------------------------------------------------- endpoints
export const api = {
  // auth
  login: (email: string, password: string) => request<LoginResponse>("POST", "/auth/login", { email, password }),
  me: () => get<User>("/auth/me"),
  users: () => get<{ id: string; email: string; name: string; is_admin: boolean }[]>("/users"),

  // workspaces
  listWorkspaces: () => get<Workspace[]>("/workspaces"),
  createWorkspace: (body: { name: string; description: string; objective: string; autonomy_level: number }) =>
    post<Workspace>("/workspaces", body),
  getWorkspace: (ws: string) => get<WorkspaceDetail>(`/workspaces/${e(ws)}`),
  updateWorkspace: (ws: string, body: Partial<Pick<Workspace, "name" | "description" | "objective" | "autonomy_level">>) =>
    patch<Workspace>(`/workspaces/${e(ws)}`, body),
  putPolicy: (ws: string, policy: Dict) => put<{ policy_version: number }>(`/workspaces/${e(ws)}/policy`, policy),
  addMember: (ws: string, email: string, role: string) =>
    post<{ user_id: string; role: string }>(`/workspaces/${e(ws)}/members`, { email, role }),
  removeMember: (ws: string, userId: string) => del<{ removed: boolean }>(`/workspaces/${e(ws)}/members/${e(userId)}`),
  workspaceAudit: (ws: string, limit = 200) => get<AuditEvent[]>(`/workspaces/${e(ws)}/audit${qs({ limit })}`),
  activity: (ws: string, afterId = 0, limit = 100) =>
    get<RunEvent[]>(`/workspaces/${e(ws)}/activity${qs({ after_id: afterId, limit })}`),

  // sources
  listSources: (ws: string) => get<Source[]>(`/workspaces/${e(ws)}/sources`),
  addSource: (ws: string, body: { kind: string; name: string; config: Dict; secret_ref?: string | null }) =>
    post<Source>(`/workspaces/${e(ws)}/sources`, body),
  discover: (ws: string, sourceId: string) => post<DiscoverResponse>(`/workspaces/${e(ws)}/sources/${e(sourceId)}/discover`),
  selectAssets: (ws: string, sourceId: string, assets: string[]) =>
    put<{ selected: string[]; loaded: Dict[] }>(`/workspaces/${e(ws)}/sources/${e(sourceId)}/selection`, { assets }),
  upload: (ws: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ path: string; bytes: number }>("POST", `/workspaces/${e(ws)}/uploads`, fd);
  },
  listAssets: (ws: string) => get<Asset[]>(`/workspaces/${e(ws)}/assets`),
  tagColumn: (assetId: string, column: string, tags: string[]) =>
    put<SourceColumn>(`/assets/${e(assetId)}/columns/${e(column)}/tags`, { tags }),
  relationships: (ws: string) => get<Relationship[]>(`/workspaces/${e(ws)}/relationships`),

  // analysis
  startRun: (ws: string, body: { objective?: string; source_ids?: string[]; autonomy_level?: number }) =>
    post<Run>(`/workspaces/${e(ws)}/analysis`, body),
  listRuns: (ws: string) => get<Run[]>(`/workspaces/${e(ws)}/analysis`),
  getRun: (ws: string, run: string) => get<RunDetail>(`/workspaces/${e(ws)}/analysis/${e(run)}`),
  controlRun: (ws: string, run: string, action: "pause" | "resume" | "cancel") =>
    post<Run>(`/workspaces/${e(ws)}/analysis/${e(run)}/${action}`),
  feedback: (ws: string, run: string, body: { text: string; kind?: string | null; target_type?: string | null; target_id?: string | null }) =>
    post<FeedbackResponse>(`/workspaces/${e(ws)}/analysis/${e(run)}/feedback`, body),
  console: (ws: string, run: string) => get<ConsoleData>(`/workspaces/${e(ws)}/analysis/${e(run)}/console`),
  agentRun: (taskId: string) =>
    get<RunTask & { messages: AgentMessage[]; tool_calls: ToolExecution[]; model_calls: ModelCall[]; queries: QueryExecution[] }>(
      `/agent-runs/${e(taskId)}`,
    ),
  patchHypothesis: (id: string, body: { statement?: string; priority?: string; status?: string }) =>
    patch<Hypothesis>(`/hypotheses/${e(id)}`, body),
  ask: (ws: string, question: string) => post<AskResponse>(`/workspaces/${e(ws)}/ask`, { question }),
  query: (ws: string, sql: string, maxRows?: number) =>
    post<QueryResult>(`/workspaces/${e(ws)}/query`, { sql, max_rows: maxRows ?? null }),

  // artifacts, insights, approvals
  listArtifacts: (ws: string, filter: { type?: string; run_id?: string } = {}) =>
    get<Artifact[]>(`/workspaces/${e(ws)}/artifacts${qs(filter)}`),
  getArtifact: (id: string) => get<ArtifactDetail>(`/artifacts/${e(id)}`),
  getQuery: (id: string) => get<QueryExecution>(`/queries/${e(id)}`),
  listInsights: (ws: string) => get<Insight[]>(`/workspaces/${e(ws)}/insights`),
  getInsight: (id: string) => get<InsightDetail>(`/insights/${e(id)}`),
  listApprovals: (ws: string, status?: string) => get<Approval[]>(`/workspaces/${e(ws)}/approvals${qs({ status })}`),
  approve: (id: string, reason?: string) => post<Approval>(`/approvals/${e(id)}/approve`, { reason: reason || null }),
  reject: (id: string, reason?: string) => post<Approval>(`/approvals/${e(id)}/reject`, { reason: reason || null }),
  rollback: (publicationId: string) =>
    post<{ removed: unknown; run_id: string | null }>(`/publications/${e(publicationId)}/rollback`),

  // admin / registry
  agents: () => get<AgentSpec[]>("/agents"),
  patchAgent: (id: string, enabled: boolean) => patch<AgentSpec>(`/agents/${e(id)}`, { enabled }),
  tools: () => get<ToolSpec[]>("/tools"),
  patchTool: (id: string, enabled: boolean) => patch<ToolSpec>(`/tools/${e(id)}`, { enabled }),
  skills: () => get<SkillSpec[]>("/skills"),
  models: () => get<ModelsView>("/admin/models"),
  usage: () => get<Usage>("/admin/usage"),
  audit: (limit = 300) => get<AuditEvent[]>(`/admin/audit${qs({ limit })}`),
};

// ----------------------------------------------------------------------------------- run events (SSE)
export interface EventStreamHandle {
  close: () => void;
}

export interface EventStreamCallbacks {
  onEvent: (ev: RunEvent) => void;
  onEnd?: (status: string | null) => void;
  onStatus?: (state: "connecting" | "open" | "reconnecting" | "closed", error?: string) => void;
}

/**
 * Subscribe to GET /api/workspaces/{ws}/analysis/{run}/events with the bearer header.
 * Reconnects with ?after_id=<last seen id> (exponential backoff, max 15 s) until the server sends
 * `event: end` (terminal run) or the caller closes the handle.
 */
export function subscribeRunEvents(ws: string, run: string, cb: EventStreamCallbacks, afterId = 0): EventStreamHandle {
  const controller = new AbortController();
  let last = afterId;
  let ended = false;
  let attempt = 0;

  const handle = (m: SSEMessage) => {
    if (m.event === "end") {
      ended = true;
      let status: string | null = null;
      try {
        status = (JSON.parse(m.data) as { status?: string }).status ?? null;
      } catch {
        /* ignore */
      }
      cb.onEnd?.(status);
      return;
    }
    if (!m.data) return;
    try {
      const ev = JSON.parse(m.data) as RunEvent;
      if (typeof ev.id === "number") {
        if (ev.id <= last) return; // duplicate after reconnect
        last = ev.id;
      }
      cb.onEvent(ev);
    } catch {
      /* malformed payload: skip */
    }
  };

  const loop = async () => {
    while (!controller.signal.aborted && !ended) {
      cb.onStatus?.(attempt === 0 ? "connecting" : "reconnecting");
      let opened = false;
      try {
        await readSSE({
          url: `${API_BASE}/workspaces/${e(ws)}/analysis/${e(run)}/events${qs({ after_id: last })}`,
          headers: authHeaders(),
          signal: controller.signal,
          onOpen: () => {
            opened = true;
            attempt = 0;
            cb.onStatus?.("open");
          },
          onMessage: handle,
        });
        if (ended || controller.signal.aborted) break;
      } catch (err) {
        if (controller.signal.aborted) break;
        const status = (err as { status?: number }).status;
        if (status === 401) {
          unauthorizedHandler?.();
          break;
        }
        if (status === 403 || status === 404) {
          cb.onStatus?.("closed", err instanceof Error ? err.message : String(err));
          return;
        }
        cb.onStatus?.("reconnecting", err instanceof Error ? err.message : String(err));
      }
      // A stream that opened and then dropped reconnects quickly; repeated failures back off.
      attempt = opened ? 1 : attempt + 1;
      const delay = Math.min(15000, 500 * 2 ** Math.min(attempt, 5));
      await new Promise((r) => setTimeout(r, delay));
    }
    cb.onStatus?.("closed");
  };
  void loop();
  return { close: () => controller.abort() };
}
