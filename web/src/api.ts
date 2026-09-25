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

export interface ScheduleInput {
  name: string;
  kind: string;
  cron: string;
  timezone: string;
  config: ScheduleConfig;
}

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

export interface SourceInput {
  kind: string;
  name: string;
  config: Dict;
  secret_ref?: string | null;
}

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

export interface CrawlInput {
  mode?: "full" | "incremental";
  include?: string[];
  exclude?: string[];
  profile?: boolean;
  enrich?: boolean;
}

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

export interface CatalogFilter {
  q?: string;
  domain?: string;
  role?: string;
  include_deprecated?: boolean;
}

export interface AssetMetadataPatch {
  business_name?: string;
  description?: string;
  reviewed?: boolean;
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
  addSource: (ws: string, body: SourceInput) => post<Source>(`/workspaces/${e(ws)}/sources`, body),
  sourceKinds: () => get<SourceKindInfo[]>("/source-kinds"),
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

  // metadata crawls & catalog
  startCrawl: (ws: string, sourceId: string, body: CrawlInput = {}) =>
    post<Crawl>(`/workspaces/${e(ws)}/sources/${e(sourceId)}/crawl`, body),
  listCrawls: (ws: string, sourceId?: string) => get<Crawl[]>(`/workspaces/${e(ws)}/crawls${qs({ source_id: sourceId })}`),
  getCrawl: (id: string) => get<Crawl>(`/crawls/${e(id)}`),
  catalog: (ws: string, filter: CatalogFilter = {}) =>
    get<CatalogAsset[]>(`/workspaces/${e(ws)}/catalog${qs({
      q: filter.q, domain: filter.domain, role: filter.role, include_deprecated: filter.include_deprecated ? "true" : undefined,
    })}`),
  curateAsset: (assetId: string, body: AssetMetadataPatch) =>
    patch<{ id: string; business_name: string | null; description: string | null; description_origin: string | null; reviewed: boolean }>(
      `/assets/${e(assetId)}/metadata`, body),

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
  explainQuery: (ws: string, sql: string, maxRows?: number) =>
    post<SqlExplanation>(`/workspaces/${e(ws)}/query/explain`, { sql, max_rows: maxRows ?? null }),

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

  // schedules (§37)
  listSchedules: (ws: string) => get<Schedule[]>(`/workspaces/${e(ws)}/schedules`),
  createSchedule: (ws: string, body: ScheduleInput) => post<Schedule>(`/workspaces/${e(ws)}/schedules`, body),
  updateSchedule: (id: string, body: Partial<Omit<ScheduleInput, "kind">> & { enabled?: boolean }) =>
    patch<Schedule>(`/schedules/${e(id)}`, body),
  deleteSchedule: (id: string) => del<{ deleted: boolean }>(`/schedules/${e(id)}`),
  runScheduleNow: (id: string) => post<ScheduleRun>(`/schedules/${e(id)}/run`),

  // monitors & alerts (§38)
  listMonitors: (ws: string) => get<Monitor[]>(`/workspaces/${e(ws)}/monitors`),
  createMonitor: (ws: string, body: { name: string; kind: string; config: MonitorConfig; auto_investigate: boolean }) =>
    post<Monitor>(`/workspaces/${e(ws)}/monitors`, body),
  updateMonitor: (id: string, body: { enabled?: boolean; auto_investigate?: boolean; config?: MonitorConfig; name?: string }) =>
    patch<Monitor>(`/monitors/${e(id)}`, body),
  evaluateMonitor: (id: string) => post<MonitorResult>(`/monitors/${e(id)}/evaluate`),
  monitorSeries: (id: string) => get<MonitorSeries>(`/monitors/${e(id)}/series`),
  listAlerts: (ws: string, status?: string) => get<Alert[]>(`/workspaces/${e(ws)}/alerts${qs({ status })}`),
  alertAction: (id: string, action: "acknowledge" | "resolve") => post<Alert>(`/alerts/${e(id)}/${action}`),
  investigateAlert: (id: string) => post<{ run_id: string | null }>(`/alerts/${e(id)}/investigate`),

  // notifications
  notifications: (unread = false) => get<AppNotification[]>(`/notifications${qs({ unread: unread ? "true" : undefined })}`),
  markNotificationsRead: (ids: number[]) => post<{ marked: number }>("/notifications/read", { ids }),

  // reports (§43)
  createReport: (ws: string, body: { run_id?: string | null; kind: string; formats: string[] }) =>
    post<Artifact>(`/workspaces/${e(ws)}/reports`, { ...body, run_id: body.run_id || null }),
  downloadReport: (id: string, format: string) => downloadFile(`/artifacts/${e(id)}/download${qs({ format })}`, `report.${format}`),

  // admin / registry
  agents: () => get<AgentSpec[]>("/agents"),
  patchAgent: (id: string, enabled: boolean) => patch<AgentSpec>(`/agents/${e(id)}`, { enabled }),
  tools: () => get<ToolSpec[]>("/tools"),
  patchTool: (id: string, enabled: boolean) => patch<ToolSpec>(`/tools/${e(id)}`, { enabled }),
  skills: () => get<SkillSpec[]>("/skills"),
  models: () => get<ModelsView>("/admin/models"),
  usage: () => get<Usage>("/admin/usage"),
  audit: (limit = 300) => get<AuditEvent[]>(`/admin/audit${qs({ limit })}`),

  // platform settings (admin only)
  adminSettings: () => get<SettingsDocument>("/admin/settings"),
  updateSettings: (patchDoc: Dict, note: string) => put<SettingsUpdateResult>("/admin/settings", { patch: patchDoc, note }),
  applyPreset: (preset: string) => post<SettingsUpdateResult>("/admin/settings/preset", { preset }),
  settingsHistory: () => get<SettingsVersion[]>("/admin/settings/history"),
  rollbackSettings: (version: number) => post<SettingsUpdateResult>("/admin/settings/rollback", { version }),
  prompts: () => get<PromptTemplate[]>("/admin/prompts"),
  tokenSavings: (days = 30) => get<TokenSavings>(`/admin/token-savings${qs({ days })}`),
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
