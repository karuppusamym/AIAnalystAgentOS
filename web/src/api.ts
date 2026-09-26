/**
 * Typed client for the AnalystOS FastAPI backend (src/analystos/api/routers/*.py).
 *
 * Every call sends `Authorization: Bearer <token>`. Errors come back as
 * {"error": {code, message, details, retryable}} and are raised as ApiError.
 */
import type { components, paths } from "./generated/openapi";
import { readSSE, type SSEMessage } from "./lib/sse";

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type Dict = Record<string, unknown>;

// ----------------------------------------------------------------------------------- response shapes
/*
 * Response shapes. The FastAPI routers return untyped dicts (no `response_model`), so the
 * OpenAPI schema says nothing about response bodies and these are maintained by hand against
 * the routers. Request bodies, paths and query parameters are generated (see "generated
 * contract" below); where a request type is named here it is an alias of the generated schema.
 */
export interface User {
  id: string;
  email: string;
  name: string;
  is_admin: boolean;
  active: boolean;
  attributes: Dict;
  created_at: string;
}

export interface UserSummary {
  id: string;
  email: string;
  name: string;
  is_admin: boolean;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

/** What the login screen offers (GET /api/auth/providers). */
export interface AuthProviders {
  password: boolean;
  oidc: { enabled: boolean; name: string; login_url: string | null };
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
  role?: string;
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
  crawl_id?: string;
  stats?: CrawlStats;
  changes?: CrawlChanges;
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
  /** representative_population (staging/snapshots.py): how the analysed rows were chosen. */
  method?: string;
  sampling?: Dict;
  truncated?: boolean;
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
  /** The proposal the approval binds to (list endpoint only; decisions return it without). */
  payload?: Dict;
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
  changes?: RunChanges | null;
  report_artifact_id?: string | null;
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
  origin?: RunOrigin | null;
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
  /** Execution-ladder rung that answered (P4-T01) and the context compiler's receipts (P4-T03). */
  answered_by?: string | null;
  context_receipts?: Dict[] | null;
  tokens_saved?: number;
}

export type AgentRunDetail = RunTask & { messages: AgentMessage[]; tool_calls: ToolExecution[]; model_calls: ModelCall[]; queries: QueryExecution[] };

export interface ConsoleData {
  messages: AgentMessage[];
  tool_calls: ToolExecution[];
  model_calls: ModelCall[];
  queries: QueryExecution[];
  cost: ConsoleCost;
}

/** Run cost block (analysis.py console). Rung counts arrive with the execution ladder (P4-T*); render when present. */
export interface ConsoleCost {
  usd: number;
  tokens: number;
  model_calls: number;
  jev_calls: number;
  failed_calls: number;
  cache_hits?: number;
  deterministic_skips?: number;
  tokens_saved?: number;
  by_rung?: Record<string, RungSpend> | null;
}

/** Spend on one rung of the deterministic-first ladder (spec v3 §4.1): L0 cache … L5 strong model. */
export interface RungSpend {
  calls?: number;
  tokens_used?: number;
  tokens_saved?: number;
  cost_usd?: number;
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

export type ChartHint = { type?: string; x?: string | null; y?: string | null } | null;

export interface AskResponse {
  sql: string;
  explanation: string | null;
  chart: ChartHint;
  model: string | null;
  attempts: { sql: string; error: string }[];
  result: QueryResult;
  status?: string;
  answered_by?: string | null;
  route?: string | null;
}

// ----------------------------------------------------------------------------------- Ask threads (P4-U02, services/ask.py)
/** One plain-language step of an Ask, streamed while it runs. */
export interface AskStage {
  key: string;
  text: string;
  at_ms: number;
  turn_id?: string;
  data?: Dict;
}

/** The refusal kinds of services/ask.py REFUSALS: one state each, with a remedy. */
export type AskRefusalKind = "needs_input" | "clarify" | "sql_rejected" | "policy_denied" | "budget_exceeded" | "no_model"
  | "no_scope" | "timeout" | "unavailable" | "failed";

export interface AskRefusal {
  kind: AskRefusalKind | string;
  title: string;
  message: string;
  remedy: string;
  details: { missing?: { name: string; type?: string; values?: string[] }[]; verified_query?: Dict | null; parameters?: Dict; [k: string]: unknown };
}

export interface AskProvenanceAsset {
  asset: string;
  asset_id: string | null;
  business_name: string | null;
  source_id: string | null;
  source_name: string | null;
  source_kind: string | null;
  execution_mode: string | null;
  freshness_at: string | null;
  row_count: number | null;
}

export interface AskProvenance {
  assets?: AskProvenanceAsset[];
  answered_by?: string | null;
  verified_query?: { id: string; name: string; pattern?: string; score?: number } | null;
  model?: string | null;
  query_id?: string | null;
  cache_hit?: boolean;
  result_hash?: string | null;
  repairs?: number;
}

export interface AskStaleness {
  state: "fresh" | "aging" | "stale" | "changed" | "unknown";
  label: string;
  data_as_of: string | null;
}

export interface AskDecisionSummary {
  id: string | null;
  purpose: string;
  backend: string;
  value: unknown;
  p?: number | null;
  model?: string | null;
  fallback_reason?: string | null;
  probabilities?: Record<string, number>;
  note?: string | null;
}

export interface AskPromotion {
  target: "verified_query" | "metric" | "monitor" | "dashboard" | "investigate" | string;
  id: string;
  status: string;
  name?: string;
  approval_id?: string | null;
  value?: unknown;
  at?: string;
  [k: string]: unknown;
}

export interface AskTurn {
  id: string;
  thread_id: string;
  workspace_id: string;
  seq: number;
  question: string;
  parameters: Dict;
  status: "running" | "answered" | "needs_input" | "clarify" | "refused" | string;
  route: string | null;
  answered_by: string | null;
  refusal: AskRefusal | null;
  sql: string | null;
  explanation: string | null;
  chart: ChartHint;
  result: QueryResult | null;
  verified_query: Dict | null;
  model: string | null;
  attempts: { sql: string; error: string }[];
  stages: AskStage[];
  decisions: AskDecisionSummary[];
  provenance: AskProvenance;
  promotions: AskPromotion[];
  latency_ms: number;
  created_at: string;
  staleness: AskStaleness;
}

export interface AskThread {
  id: string;
  workspace_id: string;
  user_id: string;
  title: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
  turn_count?: number;
}

export interface AskThreadDetail extends AskThread {
  turns: AskTurn[];
}

/** A `decision` row (decisions/store.py) recorded against the turn. */
export interface DecisionRow {
  id: string;
  purpose: string;
  authority: string;
  backend: string;
  model: string | null;
  answer: unknown;
  proposal: unknown;
  probabilities: Record<string, number>;
  confidence: number | null;
  fallback_reason: string | null;
  attempts: { backend: string; outcome: string; reason?: string | null }[];
  enforced: string[];
  subject: string | null;
  latency_ms: number;
  created_at: string;
}

export interface AskInspector {
  turn: AskTurn;
  decisions: DecisionRow[];
  model_calls: ModelCall[];
  query: QueryExecution | null;
  receipts: ContextReceipt[];
}

/**
 * One item the context compiler put in a prompt (P4-T03/K05). Pack sections carry their document,
 * path, anchor and hashes; `source` is `review:<origin>` for a document approved in the review queue.
 */
export interface ContextReceipt {
  id?: string;
  section?: string;
  kind?: string;
  name?: string;
  title?: string;
  source?: string;
  score?: number;
  trusted?: boolean;
  sha256?: string;
  document_id?: string;
  path?: string;
  anchor?: string;
  section_sha256?: string;
  rank?: number;
  via?: string;
  version?: number;
  [k: string]: unknown;
}

// ----------------------------------------------------------------------------------- knowledge studio (P4-U04)
export type KnowledgePackKind = "platform" | "workspace" | "imported";

export interface KnowledgePackInfo {
  id: string;
  kind: KnowledgePackKind;
  slug: string;
  title: string;
  read_only: boolean;
  /** True only for the workspace's own pack and an editor or owner. */
  writable: boolean;
  head_revision: number | null;
  okf_root: string;
  okf_version: string;
  git_remote: string | null;
  git_branch: string;
  origin: Dict;
  files: number;
  content_digest: string | null;
  updated_at: string;
}

export interface KnowledgeDocSummary {
  path: string;
  document_id: string;
  sha256: string;
  size: number;
  markdown: boolean;
  reserved: boolean;
  type: string | null;
  title: string;
  status?: string;
  trust_tier?: "unverified" | "machine-confirmed" | "human-reviewed" | string;
  stale?: boolean;
  kind?: string | null;
  /** `review_queue`: written by the review queue, which may replace it; `owner`: a person's content. */
  authorship?: "review_queue" | "owner";
  tags?: string[];
  problem?: string;
}

export interface KnowledgeDocList {
  pack_id: string;
  revision: number | null;
  documents: KnowledgeDocSummary[];
}

export interface VerifiedEntry {
  by: string;
  at?: string;
  [k: string]: unknown;
}

export interface KnowledgeTrust {
  tier: string;
  verified: VerifiedEntry[];
  status: string;
  stale_after: string | null;
  stale: boolean;
  trusted: boolean | null;
}

export interface KnowledgeDocument extends KnowledgeDocSummary {
  pack_id: string;
  revision: number | null;
  text: string;
  frontmatter: Dict | null;
  body: string | null;
  trust: KnowledgeTrust | null;
  sections: { anchor: string; heading: string }[];
  links: { raw: string; kind: string; target: string | null; exists: boolean }[];
}

export type KnowledgeSaveBody = Schemas["DocumentSaveIn"];

export interface KnowledgeSaveResult {
  changed: boolean;
  revision: number | null;
  document: KnowledgeDocument;
}

export interface KnowledgeRevisionInfo {
  number: number;
  parent: number | null;
  author: string;
  reason: string;
  origin: string;
  files: number;
  content_digest: string;
  created_at: string;
  added: string[];
  changed: string[];
  removed: string[];
  sha256: string | null;
  conformance_problems: number;
}

export interface SuggestionField {
  value: unknown;
  confidence: number;
  provenance: Dict;
  before?: unknown;
}

export interface KnowledgeSuggestion {
  id: string;
  kind: string;
  subject: string;
  title: string;
  path: string;
  fields: Record<string, SuggestionField>;
  confidence: number;
  origin: string;
  proposed_by: string;
  batch: string | null;
  status: "pending" | "approved" | "rejected" | "superseded";
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  revision: number | null;
  created_at: string;
}

export type ReviewDecisionBody = Schemas["ReviewDecision"];

export interface ReviewResult {
  revision: number | null;
  approved: { id: string; path: string | null; catalog?: string }[];
  rejected: { id: string; path: string | null; catalog?: string }[];
  errors: { id: string; error: string; [k: string]: unknown }[];
}

export interface KnowledgeImportReport {
  pack_id: string;
  slug: string;
  format: "atlas" | "okf" | string;
  okf_root: string;
  revision: number | null;
  changed: boolean;
  files: number;
  documents: number;
  ignored: string[];
  conformance: { code: string; path: string; detail?: string }[];
  dangling_links: number;
  verified_claims: number;
  attested_computations: number;
  manifest: Dict;
  warnings: string[];
}

export type GraphNodeKind = "table" | "dataset" | "metric" | "document" | "suggestion";

export interface KnowledgeGraphNode {
  id: string;
  kind: GraphNodeKind;
  label: string;
  status?: string;
  path?: string;
  pack_id?: string;
  pack_kind?: KnowledgePackKind;
  trust_tier?: string;
  confidence?: number;
  suggestion_id?: string;
  [k: string]: unknown;
}

export interface KnowledgeGraphEdge {
  source: string;
  target: string;
  kind: string;
  /** Governed edges are drawn solid, inferred ones dashed. */
  governed: boolean;
  why: string;
  label: string;
}

export interface KnowledgeGraph {
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
  truncated: boolean;
  governed: number;
  inferred: number;
}

export type AskPromoteBody = Schemas["AskPromoteIn"];

export interface AskStreamCallbacks {
  onStage: (s: AskStage) => void;
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
  function?: string;
  runtime?: string;
  /** Legacy seeded skill records may omit this; the API fills it with []. */
  tools?: string[];
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

export interface EffectiveRoute {
  profile: string;
  models: string[];
  mode: LLMMode;
  available: boolean;
  /** A rule-based path exists: "off"/"auto" still produce a result without a model. */
  deterministic_path: boolean;
  decision_model: boolean;
}

export interface ModelsView {
  allowlist: string[];
  profiles: Record<string, ModelProfile>;
  routing: Record<string, string>;
  providers: Record<string, { kind: string; base_url: string }>;
  available: Record<string, boolean>;
  effective?: Record<string, EffectiveRoute>;
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

// ----------------------------------------------------------------------------------- continuous (phase 3)
export type ScheduleKind = "reanalysis" | "dataset_refresh" | "report" | "monitor" | "crawl";
export type ReportKind = "executive" | "operational" | "statistical" | "exception" | "weekly_summary";
export type ReportFormat = "md" | "html" | "pdf" | "xlsx";

export interface ReportSpec {
  kind?: ReportKind | string;
  formats?: (ReportFormat | string)[];
}

/** Kind-specific schedule config (see services/schedules.py). */
export interface ScheduleConfig {
  objective?: string;
  refresh_first?: boolean;
  publish?: "skip" | "propose";
  report?: ReportSpec | null;
  kind?: ReportKind | string;
  formats?: (ReportFormat | string)[];
  run_id?: string;
  monitor_ids?: string[];
  source_ids?: string[];
  mode?: "full" | "incremental";
  include?: string[];
  exclude?: string[];
  [k: string]: unknown;
}

export interface ScheduleRunResult {
  run_id?: string;
  previous_run_id?: string | null;
  report_artifact_id?: string | null;
  changes?: { new?: number; persisting?: number; changed?: number; resolved?: number };
  /** crawl schedules: per source id, the crawl id and headline counts, or an error. */
  crawls?: Record<string, { crawl_id?: string; new?: number; changed?: number; deprecated?: number; profiled?: number; error?: string }>;
  [k: string]: unknown;
}

export interface ScheduleRun {
  id: string;
  schedule_id: string;
  workspace_id: string;
  fire_key: string;
  scheduled_for: string;
  trigger: string;
  status: string;
  result: ScheduleRunResult;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Schedule {
  id: string;
  workspace_id: string;
  name: string;
  kind: ScheduleKind | string;
  cron: string;
  timezone: string;
  config: ScheduleConfig;
  enabled: boolean;
  owner_id: string;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string;
  recent_runs?: ScheduleRun[];
}

/** Request body of POST …/schedules: the generated ScheduleIn with the typed kind-specific config. */
export type ScheduleInput = Schemas["ScheduleIn"] & { timezone: string; config: ScheduleConfig };

export type MonitorKind = "metric_threshold" | "metric_drift" | "change_point" | "forecast_deviation" | "data_quality";

export interface MonitorConfig {
  metric?: string;
  grain?: "day" | "week" | "month";
  op?: ">" | ">=" | "<" | "<=";
  value?: number;
  severity?: string;
  lookback?: number;
  z_threshold?: number;
  recent_periods?: number;
  /** forecast_deviation: interval width in standard deviations, fitted history length, optional season length. */
  z?: number;
  history?: number;
  seasonal_periods?: number;
  assets?: string[];
  [k: string]: unknown;
}

export interface MonitorResult {
  alert?: boolean;
  message?: string;
  reason?: string;
  error?: string;
  period?: string;
  value?: number;
  baseline_median?: number | null;
  z?: number | null;
  threshold?: string;
  series_tail?: [string, number][] | null;
  alert_id?: string | null;
  [k: string]: unknown;
}

export interface Monitor {
  id: string;
  workspace_id: string;
  name: string;
  kind: MonitorKind | string;
  config: MonitorConfig;
  enabled: boolean;
  auto_investigate: boolean;
  state: string;
  last_evaluated_at: string | null;
  last_result: MonitorResult;
  created_by: string;
  created_at: string;
}

export interface MonitorSeries {
  label: string;
  expression?: string;
  grain: string;
  points: [string, number][];
  query_id?: string;
}

export interface Alert {
  id: string;
  workspace_id: string;
  monitor_id: string | null;
  severity: string;
  title: string;
  message: string;
  data: Dict & { triage?: { p_material?: number; model?: string } | null };
  dedupe_key: string;
  status: string;
  investigation_run_id: string | null;
  acknowledged_by: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface NotificationLink {
  type?: "alert" | "artifact" | "run" | "schedule" | string;
  id?: string;
}

export interface AppNotification {
  id: number;
  workspace_id: string;
  user_id: string | null;
  kind: string;
  title: string;
  body: string;
  link: NotificationLink;
  read_by: string[];
  read: boolean;
  created_at: string;
}

export interface RunOrigin {
  type?: "user" | "schedule" | "alert" | string;
  schedule_id?: string;
  schedule_run_id?: string;
  previous_run_id?: string | null;
  alert_id?: string;
  publish?: "skip" | "propose" | string;
  automatic?: boolean;
  [k: string]: unknown;
}

export interface ChangedFinding {
  code?: string;
  title?: string;
  finding?: string;
  id?: string;
  effect?: number | null;
  previous_effect?: number | null;
  [k: string]: unknown;
}

export interface MetricDelta {
  name: string;
  value: number | null;
  previous_value: number | null;
  pct_change: number | null;
}

export interface RunChanges {
  previous_run_id?: string;
  new?: ChangedFinding[];
  persisting?: ChangedFinding[];
  changed?: ChangedFinding[];
  resolved?: ChangedFinding[];
  metrics?: MetricDelta[];
}

export interface ReportFile {
  sha256: string;
  bytes: number;
  mime: string;
  ext: string;
  path?: string;
}

export interface ReportContent {
  kind?: string;
  title?: string;
  report_data_hash?: string;
  files?: Record<string, ReportFile>;
  insights?: number;
  metrics?: number;
  alerts?: number;
  [k: string]: unknown;
}

export interface DownloadedFile {
  blob: Blob;
  filename: string;
}


// ----------------------------------------------------------------------------------- catalog & crawls (increment 3)
export type SourceCategory = "database" | "warehouse" | "lakehouse" | "engine" | "file" | "api";

/** One connectable kind (GET /api/source-kinds, from config/source_kinds.yaml). */
export interface SourceKindInfo {
  kind: string;
  label: string;
  category: SourceCategory | string;
  required: string[];
  optional: string[];
  default_port: number | null;
  docs: string;
  /** The credential's name; it is always supplied through secret_ref, never in config. */
  secret_field: string | null;
  execution_mode: "pushdown" | "staged" | string;
  dialect: string;
  driver_installed: boolean;
  install_hint: string | null;
  enabled: boolean;
}

/** Request body of POST …/sources (generated). */
export type SourceInput = Schemas["SourceIn"];

export interface RetypedColumn {
  name: string;
  previous_type: string;
  current_type: string;
}

export interface AssetChange {
  key: string;
  previous_fingerprint?: string | null;
  fingerprint?: string;
  added: string[];
  removed: string[];
  retyped: RetypedColumn[];
  attributes_changed: boolean;
}

export interface RenameCandidate {
  previous_key: string;
  current_key: string;
  similarity: number;
}

export interface CrawlChanges {
  new?: string[];
  changed?: AssetChange[];
  missing?: string[];
  deprecated?: string[];
  rename_candidates?: RenameCandidate[];
}

export interface CrawlStats {
  discovered?: number;
  in_scope?: number;
  truncated?: number;
  new?: number;
  changed?: number;
  unchanged?: number;
  missing?: number;
  deprecated?: number;
  renamed?: number;
  profiled?: number;
  tokens_saved?: number;
  model_calls?: number;
  [k: string]: unknown;
}

export interface CrawlLogEntry {
  at: string;
  stage: string;
  message: string;
  [k: string]: unknown;
}

export interface Crawl {
  id: string;
  source_id: string;
  workspace_id: string;
  mode: "full" | "incremental" | string;
  trigger: string;
  status: "running" | "succeeded" | "failed" | string;
  stage: string | null;
  options: { include?: string[]; exclude?: string[]; profile?: boolean; enrich?: boolean; [k: string]: unknown };
  stats: CrawlStats;
  changes: CrawlChanges;
  log: CrawlLogEntry[];
  error: string | null;
  started_by: string | null;
  started_at: string;
  finished_at: string | null;
}

/** Request body of POST …/sources/{id}/crawl (generated). */
export type CrawlInput = Schemas["CrawlIn"];

export interface PiiInfo {
  category: string | null;
  sensitivity: string;
  confidence: number;
  reasons: string[];
}

export interface GlossaryLink {
  term_id: string;
  term: string | null;
  score?: number;
  reason?: string;
}

export interface CatalogColumn {
  name: string;
  data_type: string;
  business_name: string | null;
  description: string | null;
  tags: string[];
  tags_origin: "crawler" | "user" | string;
  role: string | null;
  unit: string | null;
  pii: PiiInfo | null;
  glossary: GlossaryLink | null;
}

export type DescriptionOrigin = "source" | "rule" | "model" | "user";

export interface CatalogAsset {
  id: string;
  fq: string;
  source_id: string;
  name: string;
  business_name: string | null;
  business_name_origin?: string | null;
  description: string | null;
  description_origin: DescriptionOrigin | string | null;
  reviewed: boolean;
  selected: boolean;
  lifecycle: "active" | "deprecated" | string;
  row_count: number | null;
  role: string | null;
  domain: string | null;
  grain: string | null;
  confidence: number | null;
  last_crawled_at: string | null;
  columns: CatalogColumn[];
}

/** Query of GET …/catalog (generated parameters). */
export type CatalogFilter = NonNullable<paths["/api/workspaces/{workspace_id}/catalog"]["get"]["parameters"]["query"]>;

/** Request body of PATCH /assets/{id}/metadata (generated). */
export type AssetMetadataPatch = Schemas["AssetMetadataIn"];

export interface CuratedAsset {
  id: string;
  business_name: string | null;
  description: string | null;
  description_origin: string | null;
  reviewed: boolean;
}

export interface SqlExplanation {
  statement?: string;
  read_only?: boolean;
  summary: string;
  tables?: string[];
  ctes?: string[];
  outputs?: string[];
  joins?: { kind: string; table: string; on: string | null }[];
  filter?: string | null;
  group_by?: string[];
  aggregations?: string[];
  having?: string | null;
  order_by?: string[];
  limit?: string | null;
  window_functions?: number;
  distinct?: boolean;
  gateway: { accepted: boolean; code?: string; reason?: string };
  /** The source's plan through the gateway (EXPLAIN, never executed); only when the validator accepts. */
  plan?: { available: boolean; reason?: string; node?: string | null; estimated_rows?: number | null; total_cost?: number | null;
    nodes?: string[]; relations?: string[] };
}

// ----------------------------------------------------------------------------------- platform settings (admin)
export type LLMMode = "off" | "auto" | "always";

/** contracts/platform.py PlatformSettings: the effective admin document. */
export interface PlatformSettings {
  llm: {
    purpose_modes: Record<string, LLMMode>;
    routing_overrides: Record<string, string>;
    profile_models: Record<string, string[]>;
    disabled_models: string[];
    cache_enabled: boolean;
    cache_ttl_hours: number;
    cacheable_purposes: string[];
    max_prompt_tokens: number;
    downgrade_below_budget_fraction: number;
    compact_prompts: boolean;
    catalog_max_columns_per_table: number;
    catalog_max_tables: number;
    [k: string]: unknown;
  };
  analysis: Record<string, number | boolean | string>;
  crawl: Record<string, number | boolean | string>;
  monitors: Record<string, number | boolean | string>;
  sources: { enabled_kinds: string[]; allow_pushdown: boolean; staged_max_rows: number; [k: string]: unknown };
  features: Record<string, boolean>;
  [k: string]: unknown;
}

export interface SettingsDocument {
  version: number;
  settings: PlatformSettings;
  defaults: PlatformSettings;
  presets: Record<string, Record<string, LLMMode>>;
  schema: Dict;
}

export interface SettingsChange {
  path: string;
  from: unknown;
  to: unknown;
}

export interface SettingsUpdateResult {
  version: number;
  changes?: SettingsChange[];
  rolled_back_to?: number;
}

export interface SettingsVersion {
  version: number;
  note: string | null;
  created_by: string | null;
  created_at: string;
}

export interface PromptTemplate {
  name: string;
  version: string;
  text: string;
}

export interface TokenSavingsRow {
  calls: number;
  tokens_used: number;
  tokens_saved: number;
  cost_usd: number;
  by_status: Record<string, number>;
}

export interface TokenSavings {
  days: number;
  totals: {
    calls: number;
    tokens_used: number;
    tokens_saved: number;
    cost_usd: number;
    cache_hits: number;
    deterministic_skips: number;
    refused: number;
    saved_share: number;
  };
  by_purpose: Record<string, TokenSavingsRow>;
  /** Optional until the ladder records `answered_by` per call (spec v3 §4.1). */
  by_rung?: Record<string, RungSpend> | null;
  by_model?: Record<string, RungSpend> | null;
  missing_price?: { model: string; calls: number; tokens: number }[];
  prices_version?: string;
  cost_complete?: boolean;
}

// ----------------------------------------------------------------------------------- capabilities
export type CapabilityKind = "Agent" | "Skill" | "Tool" | "Method" | "Connector" | "Engine" | "Publisher" | "DecisionPurpose"
  | "Detector" | "Crawler" | "KnowledgePack" | "Playbook" | "Renderer";
export type SideEffect = "none" | "read_source" | "write_internal" | "write_external";
export type CertStatus = "draft" | "tested" | "certified" | "deprecated";

/** One row of GET /api/capabilities (capabilities.py `_out`). Kinds and values are open: plugins add new ones. */
export interface CapabilitySummary {
  id: string;
  kind: CapabilityKind | string;
  version: string;
  ref: string;
  summary: string;
  source: string;
  entry: string | null;
  determinism: string;
  side_effect: SideEffect | string;
  cost_class: string;
  certification: { status: CertStatus | string; evidence?: string | null };
  autonomous_ok: boolean;
  needs_approval: boolean;
  tags: string[];
  /** Enablement in the requested workspace; null without `workspace_id`. */
  enabled: boolean | null;
  bindings?: {
    capabilities: { id: string; kind: string; enabled: boolean | null; determinism: string; certification: string }[];
    tools: string[];
    model_purposes: string[];
    playbooks: string[];
    default_actions: string[];
  };
}

export interface CapabilityList {
  digest: string;
  capabilities: CapabilitySummary[];
}

/** The full manifest (GET /api/capabilities/{id}, contracts/capability.py). */
export interface CapabilityManifest {
  apiVersion?: string;
  kind: CapabilityKind | string;
  id: string;
  version: string;
  summary: string;
  entry?: string | null;
  input_schema?: Dict;
  output_schema?: Dict;
  determinism?: string;
  side_effect?: SideEffect | string;
  cost_class?: string;
  permissions?: string[];
  requires?: string[];
  certification?: { status: CertStatus | string; evidence?: string | null };
  ui?: { form?: "auto" | "none" | "custom" | string; renderer?: string | null };
  tags?: string[];
  spec?: Dict;
  source?: string;
}

export interface CapabilityReload {
  digest: string;
  previous_digest: string;
  count: number;
  problems: string[];
}

/** Invocation result: MCP invoke (mcp.py) today; the generic capability invoke shares the shape. */
export interface CapabilityInvocation {
  status: "ok" | "error" | "approval_required" | string;
  capability?: string;
  side_effect?: string;
  approval_id?: string;
  result?: unknown;
  [k: string]: unknown;
}

// ----------------------------------------------------------------------------------- build (P4-E04/E06, P4-U05)
export interface BuildTarget {
  id: string;
  workspace_id: string;
  engine: string;
  schema_name: string;
  build_role: string;
  status: string;
  provisioning: Dict;
  created_by: string;
  created_at: string;
}

export type BuildStatus = "planned" | "awaiting_approval" | "running" | "succeeded" | "failed" | "refused" | string;

export interface BuildTest {
  column: string;
  test: string;
}

export interface BuildDryRun {
  runner?: string;
  command?: string[] | string;
  ok?: boolean;
  dbt_version?: string | null;
  ossie_version?: string | null;
  tests?: { candidates?: BuildTest[]; passing?: BuildTest[]; dropped?: BuildTest[] };
  allowed_sources?: string[];
  metrics?: { metric: string; [k: string]: unknown }[];
  skipped_metrics?: { metric: string; reason: string }[];
  notes?: string[];
  [k: string]: unknown;
}

export interface BuildEstimate {
  rows?: number | null;
  columns?: number | null;
  models?: number | null;
  tests?: number | null;
  approx_bytes?: number | null;
  method?: string;
  query_id?: string | null;
}

export interface BuildRollback {
  strategy?: string;
  statements?: string[];
  restore?: string;
  previous_job_id?: string | null;
}

/** GET /api/workspaces/{id}/builds: a job without its files, manifest or logs, plus its approval status. */
export interface BuildJob {
  id: string;
  workspace_id: string;
  run_id: string;
  source_run_id: string;
  artifact_id: string | null;
  approval_id: string | null;
  approval_status?: string | null;
  engine: string;
  runner: string;
  target_schema: string;
  project_name: string;
  project_hash: string;
  plan_hash: string | null;
  relations: string[];
  dry_run: BuildDryRun;
  estimate: BuildEstimate;
  rollback: BuildRollback;
  status: BuildStatus;
  run_results: { counts?: Record<string, number>; [k: string]: unknown };
  error: string | null;
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface BuildApprovalState {
  id: string;
  status: string;
  action: string;
  payload_hash: string;
  plan_hash: string | null;
  policy_version: number;
  risk_tier: string;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  expires_at: string | null;
}

export interface BuildJobDetail extends BuildJob {
  project_files: Record<string, string>;
  manifest: Dict;
  openlineage: Dict[];
  log_tail: string;
  approval: BuildApprovalState | null;
}

export interface BuildFileDiff {
  path: string;
  status: "added" | "removed" | "modified" | "unchanged";
  lines_added: number;
  lines_removed: number;
  diff: string;
  truncated: boolean;
}

export interface BuildDiff {
  job_id: string;
  project_hash: string;
  basis: "previous_job_same_target" | "requested" | "none";
  against: { job_id: string; status: string; project_hash: string; created_at: string | null } | null;
  identical: boolean;
  files: BuildFileDiff[];
  summary: { added: number; removed: number; modified: number; unchanged: number; lines_added: number; lines_removed: number };
}

// ----------------------------------------------------------------------------------- semantic layer (P4-K03)
export type MetricProposal = Schemas["MetricProposalIn"];

export interface SemanticMetric {
  id: string;
  workspace_id: string;
  name: string;
  version: number;
  status: "draft" | "proposed" | "approved" | "deprecated" | "rejected" | string;
  definition: Dict;
  expression: string;
  normalized_expression: string;
  display_name: string | null;
  owner_id: string | null;
  proposed_by: string;
  proposed_via: string;
  run_id: string | null;
  approval_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  content_hash: string;
  created_at: string;
  updated_at: string;
}

export interface SemanticConflictRow {
  kind: "duplicate_expression" | "conflicting_definition";
  names: string[];
  metrics?: { name: string; version: number; status: string; expression: string }[];
  detail: string;
}

export interface SemanticModelView {
  model: Dict | null;
  metrics: SemanticMetric[];
  approved: string[];
  conflicts: SemanticConflictRow[];
  ossie_version: string;
}

export interface MetricProblem {
  field: string;
  message: string;
}

export interface MetricValidation {
  ok: boolean;
  problems: MetricProblem[];
  conflicts: SemanticConflictRow[];
  normalized_expression: string | null;
  existing: { version: number; status: string } | null;
}

export interface MetricProposalResult {
  metric: SemanticMetric;
  created: boolean;
  conflicts: SemanticConflictRow[];
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

// ----------------------------------------------------------------------------------- generated contract
/*
 * Paths, path parameters, query parameters and request bodies come from the generated OpenAPI
 * types (src/generated/openapi.ts, `npm run gen:api` from the committed web/openapi.json). A
 * renamed route, a removed parameter or a changed request body is a typecheck error here rather
 * than a 404 or 422 at runtime. Response bodies are not in the schema yet: the routers return
 * untyped dicts (no `response_model`), so each call names its response shape from the
 * "Response shapes" section above.
 */
export type Schemas = components["schemas"];
type Method = "get" | "post" | "put" | "patch" | "delete";
type Operation<P extends keyof paths, M extends Method> = NonNullable<paths[P][M]>;

/** Every OpenAPI path that declares method M. */
export type ApiPath<M extends Method> = {
  [P in keyof paths]: [NonNullable<paths[P][M]>] extends [never] ? never : P;
}[keyof paths];

type PathParams<O> = O extends { parameters: { path: infer X } } ? X : never;
type QueryParams<O> = O extends { parameters: { query?: infer X } } ? X : never;
type JsonBody<O> = O extends { requestBody?: infer R }
  ? NonNullable<R> extends { content: { "application/json": infer B } } ? B
    : NonNullable<R> extends { content: { "multipart/form-data": unknown } } ? FormData : never
  : never;

export type CallOptions<O> = ([PathParams<O>] extends [never] ? { path?: undefined } : { path: PathParams<O> })
  & { query?: QueryParams<O>; body?: JsonBody<O> };

function qs(params: object | undefined): string {
  if (!params) return "";
  const parts = Object.entries(params as Record<string, unknown>)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

/**
 * Build the request path (relative to API_BASE) for a typed OpenAPI route: fills `{name}`
 * segments (URL-encoded) and appends non-empty query parameters in the caller's order.
 */
export function apiPath<M extends Method, P extends ApiPath<M>>(_method: M, path: P, opts: CallOptions<Operation<P, M>>): string {
  const params = (opts.path ?? {}) as Record<string, string | number>;
  const filled = (path as string).replace(/\{(\w+)\}/g, (_m, k: string) => {
    if (params[k] === undefined) throw new Error(`missing path parameter ${k} for ${path}`);
    return encodeURIComponent(String(params[k]));
  });
  const rel = filled.startsWith(API_BASE) ? filled.slice(API_BASE.length) : filled;
  return rel + qs(opts.query as object | undefined);
}

/**
 * One typed call. The response is `unknown` because the schema does not describe it; each
 * endpoint below states its response shape. POSTs without a body send `{}`, as the routers
 * expect a JSON object.
 */
export function call<M extends Method, P extends ApiPath<M>>(method: M, path: P, opts: CallOptions<Operation<P, M>>): Promise<unknown> {
  const body = opts.body !== undefined ? opts.body : method === "post" ? {} : undefined;
  return request<unknown>(method.toUpperCase(), apiPath(method, path, opts), body);
}

const get = <P extends ApiPath<"get">>(path: P, opts: CallOptions<Operation<P, "get">>) => call("get", path, opts);
const post = <P extends ApiPath<"post">>(path: P, opts: CallOptions<Operation<P, "post">>) => call("post", path, opts);
const put = <P extends ApiPath<"put">>(path: P, opts: CallOptions<Operation<P, "put">>) => call("put", path, opts);
const patch = <P extends ApiPath<"patch">>(path: P, opts: CallOptions<Operation<P, "patch">>) => call("patch", path, opts);
const del = <P extends ApiPath<"delete">>(path: P, opts: CallOptions<Operation<P, "delete">>) => call("delete", path, opts);

/** Filename from a Content-Disposition header (attachment; filename="x.pdf"). */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      /* fall through */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : fallback;
}

/** GET a binary file with the bearer header (an <a href> cannot send it). */
export async function downloadFile(path: string, fallbackName: string): Promise<DownloadedFile> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, { method: "GET", headers: { ...authHeaders() } });
  } catch (err) {
    throw new ApiError(0, "network_error", `API unreachable: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!resp.ok) {
    const err = await parseError(resp);
    if (resp.status === 401) unauthorizedHandler?.();
    throw err;
  }
  const blob = await resp.blob();
  return { blob, filename: filenameFromDisposition(resp.headers.get("Content-Disposition"), fallbackName) };
}

/** Hand a downloaded blob to the browser as a file save, via a short-lived object URL. */
export function saveBlob(file: DownloadedFile): void {
  const url = URL.createObjectURL(file.blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = file.filename;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  try {
    a.click();
  } finally {
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
}

// ----------------------------------------------------------------------------------- endpoints
const W = (ws: string) => ({ workspace_id: ws });

export const api = {
  // auth
  login: (email: string, password: string) => post("/api/auth/login", { body: { email, password } }) as Promise<LoginResponse>,
  me: () => get("/api/auth/me", {}) as Promise<User>,
  authProviders: () => get("/api/auth/providers", {}) as Promise<AuthProviders>,
  users: () => get("/api/users", {}) as Promise<UserSummary[]>,

  // workspaces
  listWorkspaces: () => get("/api/workspaces", {}) as Promise<Workspace[]>,
  createWorkspace: (body: Schemas["WorkspaceIn"]) => post("/api/workspaces", { body }) as Promise<Workspace>,
  getWorkspace: (ws: string) =>
    get("/api/workspaces/{workspace_id}", { path: W(ws) }) as Promise<WorkspaceDetail>,
  updateWorkspace: (ws: string, body: Schemas["WorkspacePatch"]) =>
    patch("/api/workspaces/{workspace_id}", { path: W(ws), body }) as Promise<Workspace>,
  setWorkspaceStatus: (ws: string, status: "active" | "disabled") =>
    patch("/api/workspaces/{workspace_id}", { path: W(ws), body: { status } }) as Promise<Workspace>,
  putPolicy: (ws: string, policy: Dict) =>
    put("/api/workspaces/{workspace_id}/policy", { path: W(ws), body: policy }) as Promise<{ policy_version: number }>,
  addMember: (ws: string, email: string, role: string) =>
    post("/api/workspaces/{workspace_id}/members", { path: W(ws), body: { email, role } }) as Promise<{ user_id: string; role: string }>,
  removeMember: (ws: string, userId: string) =>
    del("/api/workspaces/{workspace_id}/members/{user_id}", { path: { workspace_id: ws, user_id: userId } }) as Promise<{ removed: boolean }>,
  workspaceAudit: (ws: string, limit = 200) =>
    get("/api/workspaces/{workspace_id}/audit", { path: W(ws), query: { limit } }) as Promise<AuditEvent[]>,
  activity: (ws: string, afterId = 0, limit = 100) =>
    get("/api/workspaces/{workspace_id}/activity", { path: W(ws), query: { after_id: afterId, limit } }) as Promise<RunEvent[]>,

  // sources
  listSources: (ws: string) => get("/api/workspaces/{workspace_id}/sources", { path: W(ws) }) as Promise<Source[]>,
  addSource: (ws: string, body: SourceInput) =>
    post("/api/workspaces/{workspace_id}/sources", { path: W(ws), body }) as Promise<Source>,
  sourceKinds: () => get("/api/source-kinds", {}) as Promise<SourceKindInfo[]>,
  discover: (ws: string, sourceId: string) =>
    post("/api/workspaces/{workspace_id}/sources/{source_id}/discover", { path: { workspace_id: ws, source_id: sourceId } }) as Promise<DiscoverResponse>,
  selectAssets: (ws: string, sourceId: string, assets: string[]) =>
    put("/api/workspaces/{workspace_id}/sources/{source_id}/selection", { path: { workspace_id: ws, source_id: sourceId }, body: { assets } }) as Promise<{ selected: string[]; loaded: Dict[] }>,
  upload: (ws: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return post("/api/workspaces/{workspace_id}/uploads", { path: W(ws), body: fd }) as Promise<{ path: string; bytes: number }>;
  },
  listAssets: (ws: string) => get("/api/workspaces/{workspace_id}/assets", { path: W(ws) }) as Promise<Asset[]>,
  tagColumn: (assetId: string, column: string, tags: string[]) =>
    put("/api/assets/{asset_id}/columns/{column}/tags", { path: { asset_id: assetId, column }, body: { tags } }) as Promise<SourceColumn>,
  relationships: (ws: string) =>
    get("/api/workspaces/{workspace_id}/relationships", { path: W(ws) }) as Promise<Relationship[]>,

  // metadata crawls & catalog
  startCrawl: (ws: string, sourceId: string, body: CrawlInput = {}) =>
    post("/api/workspaces/{workspace_id}/sources/{source_id}/crawl", { path: { workspace_id: ws, source_id: sourceId }, body }) as Promise<Crawl>,
  listCrawls: (ws: string, sourceId?: string) =>
    get("/api/workspaces/{workspace_id}/crawls", { path: W(ws), query: { source_id: sourceId } }) as Promise<Crawl[]>,
  getCrawl: (id: string) => get("/api/crawls/{crawl_id}", { path: { crawl_id: id } }) as Promise<Crawl>,
  catalog: (ws: string, filter: CatalogFilter = {}) =>
    get("/api/workspaces/{workspace_id}/catalog", {
      path: W(ws),
      query: { q: filter.q, domain: filter.domain, role: filter.role, include_deprecated: filter.include_deprecated ? true : undefined },
    }) as Promise<CatalogAsset[]>,
  curateAsset: (assetId: string, body: AssetMetadataPatch) =>
    patch("/api/assets/{asset_id}/metadata", { path: { asset_id: assetId }, body }) as Promise<CuratedAsset>,

  // analysis
  startRun: (ws: string, body: Schemas["RunIn"]) =>
    post("/api/workspaces/{workspace_id}/analysis", { path: W(ws), body }) as Promise<Run>,
  listRuns: (ws: string) => get("/api/workspaces/{workspace_id}/analysis", { path: W(ws) }) as Promise<Run[]>,
  getRun: (ws: string, run: string) =>
    get("/api/workspaces/{workspace_id}/analysis/{run_id}", { path: { workspace_id: ws, run_id: run } }) as Promise<RunDetail>,
  controlRun: (ws: string, run: string, action: "pause" | "resume" | "cancel") =>
    post(`/api/workspaces/{workspace_id}/analysis/{run_id}/${action}`, { path: { workspace_id: ws, run_id: run } }) as Promise<Run>,
  feedback: (ws: string, run: string, body: Schemas["FeedbackIn"]) =>
    post("/api/workspaces/{workspace_id}/analysis/{run_id}/feedback", { path: { workspace_id: ws, run_id: run }, body }) as Promise<FeedbackResponse>,
  console: (ws: string, run: string) =>
    get("/api/workspaces/{workspace_id}/analysis/{run_id}/console", { path: { workspace_id: ws, run_id: run } }) as Promise<ConsoleData>,
  agentRun: (taskId: string) =>
    get("/api/agent-runs/{task_id}", { path: { task_id: taskId } }) as Promise<AgentRunDetail>,
  patchHypothesis: (id: string, body: Schemas["HypothesisPatch"]) =>
    patch("/api/hypotheses/{hypothesis_id}", { path: { hypothesis_id: id }, body }) as Promise<Hypothesis>,
  ask: (ws: string, question: string) =>
    post("/api/workspaces/{workspace_id}/ask", { path: W(ws), body: { question } }) as Promise<AskResponse>,
  query: (ws: string, sql: string, maxRows?: number) =>
    post("/api/workspaces/{workspace_id}/query", { path: W(ws), body: { sql, max_rows: maxRows ?? null } }) as Promise<QueryResult>,
  explainQuery: (ws: string, sql: string, maxRows?: number) =>
    post("/api/workspaces/{workspace_id}/query/explain", { path: W(ws), body: { sql, max_rows: maxRows ?? null } }) as Promise<SqlExplanation>,

  // artifacts, insights, approvals
  listArtifacts: (ws: string, filter: { type?: string; run_id?: string } = {}) =>
    get("/api/workspaces/{workspace_id}/artifacts", { path: W(ws), query: { type: filter.type, run_id: filter.run_id } }) as Promise<Artifact[]>,
  getArtifact: (id: string) => get("/api/artifacts/{artifact_id}", { path: { artifact_id: id } }) as Promise<ArtifactDetail>,
  getQuery: (id: string) => get("/api/queries/{query_id}", { path: { query_id: id } }) as Promise<QueryExecution>,
  listInsights: (ws: string) => get("/api/workspaces/{workspace_id}/insights", { path: W(ws) }) as Promise<Insight[]>,
  getInsight: (id: string) => get("/api/insights/{insight_id}", { path: { insight_id: id } }) as Promise<InsightDetail>,
  listApprovals: (ws: string, status?: string) =>
    get("/api/workspaces/{workspace_id}/approvals", { path: W(ws), query: { status } }) as Promise<Approval[]>,
  approve: (id: string, reason?: string) =>
    post("/api/approvals/{approval_id}/approve", { path: { approval_id: id }, body: { reason: reason || null } }) as Promise<Approval>,
  reject: (id: string, reason?: string) =>
    post("/api/approvals/{approval_id}/reject", { path: { approval_id: id }, body: { reason: reason || null } }) as Promise<Approval>,
  rollback: (publicationId: string) =>
    post("/api/publications/{publication_id}/rollback", { path: { publication_id: publicationId } }) as Promise<{ removed: unknown; run_id: string | null }>,

  // schedules (§37)
  listSchedules: (ws: string) => get("/api/workspaces/{workspace_id}/schedules", { path: W(ws) }) as Promise<Schedule[]>,
  createSchedule: (ws: string, body: ScheduleInput) =>
    post("/api/workspaces/{workspace_id}/schedules", { path: W(ws), body }) as Promise<Schedule>,
  updateSchedule: (id: string, body: Schemas["SchedulePatch"]) =>
    patch("/api/schedules/{schedule_id}", { path: { schedule_id: id }, body }) as Promise<Schedule>,
  deleteSchedule: (id: string) =>
    del("/api/schedules/{schedule_id}", { path: { schedule_id: id } }) as Promise<{ deleted: boolean }>,
  runScheduleNow: (id: string) =>
    post("/api/schedules/{schedule_id}/run", { path: { schedule_id: id } }) as Promise<ScheduleRun>,

  // monitors & alerts (§38)
  listMonitors: (ws: string) => get("/api/workspaces/{workspace_id}/monitors", { path: W(ws) }) as Promise<Monitor[]>,
  createMonitor: (ws: string, body: Schemas["MonitorIn"] & { config: MonitorConfig }) =>
    post("/api/workspaces/{workspace_id}/monitors", { path: W(ws), body }) as Promise<Monitor>,
  updateMonitor: (id: string, body: Schemas["MonitorPatch"]) =>
    patch("/api/monitors/{monitor_id}", { path: { monitor_id: id }, body }) as Promise<Monitor>,
  evaluateMonitor: (id: string) =>
    post("/api/monitors/{monitor_id}/evaluate", { path: { monitor_id: id } }) as Promise<MonitorResult>,
  monitorSeries: (id: string) =>
    get("/api/monitors/{monitor_id}/series", { path: { monitor_id: id } }) as Promise<MonitorSeries>,
  listAlerts: (ws: string, status?: string) =>
    get("/api/workspaces/{workspace_id}/alerts", { path: W(ws), query: { status } }) as Promise<Alert[]>,
  alertAction: (id: string, action: "acknowledge" | "resolve") =>
    post("/api/alerts/{alert_id}/{action}", { path: { alert_id: id, action } }) as Promise<Alert>,
  investigateAlert: (id: string) =>
    post("/api/alerts/{alert_id}/{action}", { path: { alert_id: id, action: "investigate" } }) as Promise<{ run_id: string | null }>,

  // notifications
  notifications: (unread = false) =>
    get("/api/notifications", { query: { unread: unread ? true : undefined } }) as Promise<AppNotification[]>,
  markNotificationsRead: (ids: number[]) =>
    post("/api/notifications/read", { body: { ids } }) as Promise<{ marked: number }>,

  // reports (§43)
  createReport: (ws: string, body: { run_id?: string | null; kind: string; formats: string[] }) =>
    post("/api/workspaces/{workspace_id}/reports", { path: W(ws), body: { ...body, run_id: body.run_id || null } }) as Promise<Artifact>,
  downloadReport: (id: string, format: string) =>
    downloadFile(apiPath("get", "/api/artifacts/{artifact_id}/download", { path: { artifact_id: id }, query: { format } }), `report.${format}`),

  // admin / registry
  agents: () => get("/api/agents", {}) as Promise<AgentSpec[]>,
  patchAgent: (id: string, enabled: boolean) =>
    patch("/api/agents/{agent_id}", { path: { agent_id: id }, body: { enabled } }) as Promise<AgentSpec>,
  tools: () => get("/api/tools", {}) as Promise<ToolSpec[]>,
  patchTool: (id: string, enabled: boolean) =>
    patch("/api/tools/{tool_id}", { path: { tool_id: id }, body: { enabled } }) as Promise<ToolSpec>,
  skills: () => get("/api/skills", {}) as Promise<SkillSpec[]>,
  models: () => get("/api/admin/models", {}) as Promise<ModelsView>,
  usage: () => get("/api/admin/usage", {}) as Promise<Usage>,
  audit: (limit = 300) => get("/api/admin/audit", { query: { limit } }) as Promise<AuditEvent[]>,

  // platform settings (admin only)
  adminSettings: () => get("/api/admin/settings", {}) as Promise<SettingsDocument>,
  updateSettings: (patchDoc: Dict, note: string) =>
    put("/api/admin/settings", { body: { patch: patchDoc, note } }) as Promise<SettingsUpdateResult>,
  applyPreset: (preset: string) =>
    post("/api/admin/settings/preset", { body: { preset } }) as Promise<SettingsUpdateResult>,
  settingsHistory: () => get("/api/admin/settings/history", {}) as Promise<SettingsVersion[]>,
  rollbackSettings: (version: number) =>
    post("/api/admin/settings/rollback", { body: { version } }) as Promise<SettingsUpdateResult>,
  prompts: () => get("/api/admin/prompts", {}) as Promise<PromptTemplate[]>,
  tokenSavings: (days = 30) => get("/api/admin/token-savings", { query: { days } }) as Promise<TokenSavings>,

  // capability registry (P4-X01)
  listCapabilities: (filter: { kind?: string; workspace_id?: string } = {}) =>
    get("/api/capabilities", { query: filter }) as Promise<CapabilityList>,
  getCapability: (id: string) =>
    get("/api/capabilities/{capability_id}", { path: { capability_id: id } }) as Promise<CapabilityManifest>,
  setCapabilityEnabled: (ws: string, id: string, enabled: boolean) =>
    put("/api/workspaces/{workspace_id}/capabilities/{capability_id}", { path: { workspace_id: ws, capability_id: id }, body: { enabled } }) as
      Promise<Dict>,
  reloadCapabilities: () => post("/api/admin/capabilities/reload", {}) as Promise<CapabilityReload>,
  mcpCapabilities: (ws: string) => get("/api/workspaces/{workspace_id}/mcp/capabilities", { path: W(ws) }) as Promise<CapabilityManifest[]>,
  invokeMcpTool: (ws: string, server: string, tool: string, args: Dict) =>
    post("/api/workspaces/{workspace_id}/mcp/servers/{server_name}/tools/{tool_name}/invoke",
      { path: { workspace_id: ws, server_name: server, tool_name: tool }, body: { arguments: args } }) as Promise<CapabilityInvocation>,
  /** Built-in or plugin capability (capabilities/invoke.py): read-only runs now; a side effect answers 202 + approval. */
  invokeCapability: (ws: string, id: string, args: Dict, approvalId?: string) =>
    post("/api/workspaces/{workspace_id}/capabilities/{capability_id}/invoke",
      { path: { workspace_id: ws, capability_id: id }, body: approvalId ? { arguments: args, approval_id: approvalId } : { arguments: args } }) as Promise<CapabilityInvocation>,
  /** P4-T09: accept or dismiss a finding (labels the decisions behind it for calibration). */
  findingOutcome: (insightId: string, signal: "accept" | "dismiss") =>
    post("/api/insights/{insight_id}/outcome", { path: { insight_id: insightId }, body: { signal } }) as
      Promise<{ insight: string; signal: string; labelled_decisions: number }>,

  // build (P4-E04/E06): targets, elt_build runs and their jobs; approval goes through the inbox
  buildTargets: (ws: string) => get("/api/workspaces/{workspace_id}/build-targets", { path: W(ws) }) as Promise<BuildTarget[]>,
  designateBuildTarget: (ws: string, schema: string) =>
    post("/api/workspaces/{workspace_id}/build-targets", { path: W(ws), body: { schema_name: schema } }) as Promise<BuildTarget>,
  startBuild: (ws: string, fromRunId: string, targetSchema: string) =>
    post("/api/workspaces/{workspace_id}/builds", { path: W(ws), body: { from_run_id: fromRunId, target_schema: targetSchema } }) as
      Promise<{ run_id: string; status: string; playbook: string }>,
  listBuilds: (ws: string) => get("/api/workspaces/{workspace_id}/builds", { path: W(ws) }) as Promise<BuildJob[]>,
  getBuild: (id: string) => get("/api/builds/{job_id}", { path: { job_id: id } }) as Promise<BuildJobDetail>,
  buildDiff: (id: string, against?: string) =>
    get("/api/builds/{job_id}/diff", { path: { job_id: id }, query: { against } }) as Promise<BuildDiff>,

  // semantic layer (P4-K03): KPI proposals, validation and the separation-of-duties approve route
  semanticModel: (ws: string) => get("/api/workspaces/{workspace_id}/semantic", { path: W(ws) }) as Promise<SemanticModelView>,
  metricVersions: (ws: string, name: string) =>
    get("/api/workspaces/{workspace_id}/semantic/metrics/{name}", { path: { workspace_id: ws, name } }) as Promise<SemanticMetric[]>,
  validateMetric: (ws: string, body: MetricProposal) =>
    post("/api/workspaces/{workspace_id}/semantic/metrics/validate", { path: W(ws), body }) as Promise<MetricValidation>,
  proposeMetric: (ws: string, body: MetricProposal) =>
    post("/api/workspaces/{workspace_id}/semantic/metrics", { path: W(ws), body }) as Promise<MetricProposalResult>,
  decideMetric: (ws: string, name: string, approve: boolean, version?: number, reason?: string) =>
    post(approve ? "/api/workspaces/{workspace_id}/semantic/metrics/{name}/approve" : "/api/workspaces/{workspace_id}/semantic/metrics/{name}/reject",
      { path: { workspace_id: ws, name }, body: { version: version ?? null, reason: reason || null } }) as Promise<SemanticMetric>,

  // knowledge studio (P4-U04): packs, documents, revisions, review queue, import/export, graph
  knowledgePacks: (ws: string) => get("/api/workspaces/{workspace_id}/knowledge/packs", { path: W(ws) }) as Promise<KnowledgePackInfo[]>,
  knowledgeDocuments: (ws: string, pack: string, revision?: number) =>
    get("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/documents", { path: { workspace_id: ws, pack_id: pack }, query: { revision } }) as
      Promise<KnowledgeDocList>,
  knowledgeDocument: (ws: string, pack: string, path: string, revision?: number) =>
    get("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/document", { path: { workspace_id: ws, pack_id: pack }, query: { path, revision } }) as
      Promise<KnowledgeDocument>,
  saveKnowledgeDocument: (ws: string, pack: string, body: KnowledgeSaveBody) =>
    put("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/document", { path: { workspace_id: ws, pack_id: pack }, body }) as
      Promise<KnowledgeSaveResult>,
  knowledgeRevisions: (ws: string, pack: string, path?: string) =>
    get("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/revisions", { path: { workspace_id: ws, pack_id: pack }, query: { path } }) as
      Promise<KnowledgeRevisionInfo[]>,
  locateKnowledge: (ws: string, documentId: string) =>
    get("/api/workspaces/{workspace_id}/knowledge/locate", { path: W(ws), query: { document_id: documentId } }) as
      Promise<{ document_id: string; pack_id: string; path: string; title: string; revision: number }>,
  knowledgeSuggestions: (ws: string, status: string = "pending") =>
    get("/api/workspaces/{workspace_id}/knowledge/suggestions", { path: W(ws), query: { status } }) as Promise<KnowledgeSuggestion[]>,
  reviewSuggestions: (ws: string, decisions: ReviewDecisionBody[]) =>
    post("/api/workspaces/{workspace_id}/knowledge/suggestions/review", { path: W(ws), body: { decisions } }) as Promise<ReviewResult>,
  importKnowledge: (ws: string, file: File, slug: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("slug", slug);
    return post("/api/workspaces/{workspace_id}/knowledge/import", { path: W(ws), body: fd }) as Promise<KnowledgeImportReport>;
  },
  exportKnowledge: (ws: string, pack: string, slug: string, revision?: number) =>
    downloadFile(apiPath("get", "/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/export", { path: { workspace_id: ws, pack_id: pack },
      query: { revision } }), `${slug}.okf.zip`),
  requestKnowledgePush: (ws: string, pack: string) =>
    post("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/push/request", { path: { workspace_id: ws, pack_id: pack } }) as Promise<Approval>,
  pushKnowledge: (ws: string, pack: string, approvalId: string) =>
    post("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/push", { path: { workspace_id: ws, pack_id: pack }, body: { approval_id: approvalId } }) as
      Promise<{ revision: number; commit: string; remote: string; branch: string }>,
  knowledgeGraph: (ws: string) => get("/api/workspaces/{workspace_id}/knowledge/graph", { path: W(ws) }) as Promise<KnowledgeGraph>,

  // dashboards: publishing is proposal-based (returns the pending, hash-bound proposal; decided in the inbox)
  publishDashboard: (artifactId: string) =>
    post("/api/artifacts/{artifact_id}/publish", { path: { artifact_id: artifactId } }) as Promise<Approval>,

  // Ask threads (P4-U02)
  askThreads: (ws: string, q?: string) =>
    get("/api/workspaces/{workspace_id}/ask/threads", { path: W(ws), query: { q: q || undefined } }) as Promise<AskThread[]>,
  createAskThread: (ws: string, title?: string) =>
    post("/api/workspaces/{workspace_id}/ask/threads", { path: W(ws), body: { title: title ?? null } }) as Promise<AskThreadDetail>,
  askThread: (id: string) => get("/api/ask/threads/{thread_id}", { path: { thread_id: id } }) as Promise<AskThreadDetail>,
  patchAskThread: (id: string, body: Schemas["AskThreadPatch"]) =>
    patch("/api/ask/threads/{thread_id}", { path: { thread_id: id }, body }) as Promise<AskThread>,
  askTurn: (threadId: string, question: string, parameters?: Dict) =>
    post("/api/ask/threads/{thread_id}/turns", { path: { thread_id: threadId }, body: { question, parameters: parameters ?? null } }) as
      Promise<AskTurn>,
  askInspector: (turnId: string) => get("/api/ask/turns/{turn_id}/inspector", { path: { turn_id: turnId } }) as Promise<AskInspector>,
  promoteTurn: (turnId: string, body: AskPromoteBody) =>
    post("/api/ask/turns/{turn_id}/promote", { path: { turn_id: turnId }, body }) as Promise<AskPromotion>,
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
          url: API_BASE + apiPath("get", "/api/workspaces/{workspace_id}/analysis/{run_id}/events", {
            path: { workspace_id: ws, run_id: run }, query: { after_id: last } }),
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

/**
 * Ask in a thread with the stages streamed (POST + `Accept: text/event-stream`): `stage` events as
 * the Ask runs, then `turn` (the persisted answer or refusal). Rejects with ApiError on an `error`
 * event, a non-2xx status or a stream that ends without the answer.
 */
export async function streamAskTurn(threadId: string, question: string, parameters: Dict | undefined, cb: AskStreamCallbacks,
  signal?: AbortSignal): Promise<AskTurn> {
  let turn: AskTurn | null = null;
  let failure: ApiError | null = null;
  try {
    await readSSE({
      url: API_BASE + apiPath("post", "/api/ask/threads/{thread_id}/turns", { path: { thread_id: threadId } }),
      method: "POST", body: { question, parameters: parameters ?? null }, headers: authHeaders(),
      signal: signal ?? new AbortController().signal,
      onMessage: (m) => {
        let data: unknown = null;
        try {
          data = m.data ? JSON.parse(m.data) : null;
        } catch {
          return;
        }
        if (m.event === "stage") cb.onStage(data as AskStage);
        else if (m.event === "turn") turn = data as AskTurn;
        else if (m.event === "error") {
          const e = (data as { error?: { code?: string; message?: string; details?: Dict } }).error ?? {};
          failure = new ApiError(422, e.code ?? "error", e.message ?? "The question could not be asked", e.details ?? {});
        }
      },
    });
  } catch (err) {
    const status = (err as { status?: number }).status;
    if (status === 401) unauthorizedHandler?.();
    throw new ApiError(status ?? 0, status ? `http_${status}` : "network_error", err instanceof Error ? err.message : String(err));
  }
  if (failure) throw failure;
  if (!turn) throw new ApiError(0, "stream_incomplete", "The answer stream ended before the answer arrived");
  return turn;
}
