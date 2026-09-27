/**
 * Pipelines in memory (docs/30-runbooks/05-pipelines.md): two versions of a pipeline (for the
 * transformation diff), a dry run with join diagnostics, contract gates, quarantined rows and
 * reconciliation that asks for a materialize approval, materialization as a new table version,
 * rollback, and a blocked run and a stale destination for Operate.
 */
import type { Materialization, Pipeline, PipelineRun } from "../api";
import { approvalStatus, requestApproval } from "./mockApprovals";

const T = "2026-09-26T13:00:00Z";
const HOURS_AGO = (h: number) => new Date(Date.parse(T) - h * 3_600_000).toISOString();
export const PIPELINE = "pip_2";
export const DRY_RUN = "prn_1";
export const MAT_APPROVAL = "apr_mat";

type Reply = { status: number; body: unknown } | null;
const ok = (body: unknown, status = 200): Reply => ({ status, body });
const err = (status: number, code: string, message: string, details: Record<string, unknown> = {}): Reply =>
  ({ status, body: { error: { code, message, retryable: false, details } } });

const SPEC_V1 = {
  type: "pipeline", name: "p1_clean", description: "Clean P1 incidents", recipes: [{ name: "p1_incidents_clean", version: 1 }],
  inputs: [{ asset: "stg_sn.incident", max_age_hours: 24 }], output: { recipe: "p1_incidents_clean", output: "clean", grain: ["sys_id"], keys: ["sys_id"] },
  checks: [{ name: "row_count", func: "count", input_node: "src_incident", tolerance_pct: 1 }], freshness: { max_age_hours: 24 },
  destination: { engine: "postgres:analytics", schema: "aos_out", table: "p1_clean" },
};
const SPEC_V2 = {
  ...SPEC_V1, recipes: [{ name: "p1_incidents_clean", version: 2 }],
  inputs: [...SPEC_V1.inputs, { asset: "stg_sn.sys_user_group", max_age_hours: 168 }],
  joins: [{ node: "join_groups", cardinality: "many_to_one", max_unmatched_pct: 1, max_row_multiplication: 1 }],
  checks: [...SPEC_V1.checks, { name: "hours_total", func: "sum", input_node: "src_incident", input_column: "resolution_hours", output_column: "resolution_hours", tolerance_pct: 0.5 }],
};

const pipe = (id: string, version: number, spec: Record<string, unknown>, status = "published"): Pipeline => ({
  id, workspace_id: "ws_demo", name: "p1_clean", version, status, spec, spec_hash: `ps_${id}`, created_by: "usr_admin", created_at: HOURS_AGO(100 - version),
  published_at: status === "published" ? HOURS_AGO(99 - version) : null,
});

const mat = (id: string, version: number, status: string, promotedHoursAgo: number, previous: string | null, rows = 4150): Materialization => ({
  id, workspace_id: "ws_demo", destination_id: "wdst_1", schema_name: "aos_out", table_name: "p1_clean", version, version_table: `p1_clean__v${version}`,
  pipeline_id: PIPELINE, pipeline_run_id: `prn_old_${version}`, approval_id: `apr_old_${version}`, status, row_count: rows,
  content_fingerprint: `cf${version}0a1b2c3d4e5f`, previous_id: previous, checkpoint: null, error: null, created_by: "usr_admin",
  created_at: HOURS_AGO(promotedHoursAgo), promoted_at: status === "failed" ? null : HOURS_AGO(promotedHoursAgo),
});

export function dryRun(): PipelineRun {
  return {
    id: DRY_RUN, workspace_id: "ws_demo", pipeline_id: PIPELINE, pipeline_name: "p1_clean", pipeline_version: 2, spec_hash: "ps_pip_2", mode: "dry_run",
    status: "awaiting_approval", recipes: { p1_incidents_clean: 2 }, plan: { engine: "duckdb" },
    manifest: { scope_hash: "sc_1", engine: "duckdb", cross_source: false, join_strategy: "pushdown: one governed statement on the single source",
      sources: { src_sn: { source_id: "src_sn", name: "ServiceNow", read_identity: "query gateway as workspace reader role aos_r_ws_demo",
        assets: { "stg_sn.incident": { snapshot: "snap_inc_77", snapshot_rows: 4210 }, "stg_sn.sys_user_group": { snapshot: "snap_grp_12", snapshot_rows: 12 } } } } },
    sql: { dialect: "postgres", output: "SELECT i.sys_id, i.priority, g.name AS assignment_group, i.resolution_hours\nFROM stg_sn.incident i\nLEFT JOIN stg_sn.sys_user_group g ON g.sys_id = i.assignment_group",
      preflight: { join_groups: "SELECT count(*) … FROM stg_sn.incident i LEFT JOIN stg_sn.sys_user_group g …" } },
    checks: [
      { check: "input_version", asset: "stg_sn.incident", pinned: null, current: "cf_inc", ok: true },
      { check: "input_freshness", asset: "stg_sn.incident", age_hours: 3.2, max_age_hours: 24, ok: true },
      { check: "join_cardinality", join: "join_groups", declared: "many_to_one", observed: "many_to_one", ok: true },
      { check: "unmatched_rows", join: "join_groups", unmatched_left_pct: 0.0713, max_unmatched_pct: 1, ok: true },
      { check: "fanout", join: "join_groups", row_multiplication: 1, max_row_multiplication: 1, ok: true },
      { check: "gate", gate: "not_null:resolved_at", severity: "drop", status: "dropped", failed_rows: 20, ok: true },
      { check: "gate", gate: "accepted_values:priority", severity: "warn", status: "warned", failed_rows: 4, ok: true },
      { check: "complete_output", truncated: false, ok: true },
      { check: "aggregate_reconciliation", name: "row_count", difference_pct: 0.4751, tolerance_pct: 1, ok: true },
      { check: "aggregate_reconciliation", name: "hours_total", difference_pct: 0.31, tolerance_pct: 0.5, ok: true },
      { check: "budget", rows_read: 4222, queries: 6, ok: true },
    ],
    reconciliation: {
      inputs: { src_incident: 4210, src_groups: 12 }, input_rows: 4222, output_rows: 4190, rejected_rows: 20, dropped_rows: 20, blocked: false,
      unmatched: { join_groups: { unmatched_left_rows: 3, unmatched_left_pct: 0.0713, unmatched_left_keys: 2, unmatched_right_keys: 1, row_multiplication: 1 } },
      aggregates: [
        { name: "row_count", func: "count", input: { node: "src_incident", column: null, value: 4210 }, output: { column: null, value: 4190 }, difference: -20,
          difference_pct: 0.4751, tolerance_pct: 1, ok: true },
        { name: "hours_total", func: "sum", input: { node: "src_incident", column: "resolution_hours", value: 25310.5 }, output: { column: "resolution_hours", value: 25232.1 },
          difference: -78.4, difference_pct: 0.31, tolerance_pct: 0.5, ok: true },
      ],
    },
    candidate: { snapshot: "cand_9f1", row_count: 4190, columns: [{ name: "sys_id" }, { name: "priority" }, { name: "assignment_group" }, { name: "resolution_hours" }],
      keys: ["sys_id"], preview: [["INC001", "1", "Network", 9.5], ["INC002", "1", "Desktop", 4.0]],
      gates: { blocked: false, kept_rows: 4190, dropped_rows: 20, warnings: ["accepted_values:priority: 4 rows outside 1..5"],
        gates: [{ gate: "not_null:resolved_at", severity: "drop", status: "dropped", failed_rows: 20 }, { gate: "accepted_values:priority", severity: "warn", status: "warned", failed_rows: 4 }] } },
    recipe_run_ids: [], query_ids: ["qry_p1", "qry_p2"], plan_hash: "plh_1a2b3c4d5e6f", approval_id: MAT_APPROVAL, error: null, created_by: "usr_admin",
    created_at: T, finished_at: T,
  };
}

interface PipeState { pipelines: Pipeline[]; runs: PipelineRun[]; mats: Materialization[] }
let s: PipeState;

function seed(): PipeState {
  const blocked: PipelineRun = { ...dryRun(), id: "prn_0", status: "blocked", approval_id: null, created_at: HOURS_AGO(20), finished_at: HOURS_AGO(20),
    error: "gate not_null:sys_id: 3 rows failed a blocking gate", candidate: null,
    checks: [{ check: "gate", gate: "not_null:sys_id", severity: "fail", status: "failed", failed_rows: 3, ok: false }],
    reconciliation: { ...dryRun().reconciliation!, blocked: true, output_rows: 0, rejected_rows: 4207 } };
  return { pipelines: [pipe("pip_2", 2, SPEC_V2), pipe("pip_1", 1, SPEC_V1)], runs: [blocked],
    mats: [mat("mat_2", 2, "promoted", 30, "mat_1"), mat("mat_1", 1, "superseded", 80, null, 4102)] };
}
s = seed();

export function resetPipelines(): void {
  s = seed();
}

export function pipelineRoute(m: string, p: string, url: URL, W: string, body: Record<string, unknown>): Reply {
  if (m === "GET" && p === `${W}/pipelines`) return ok(s.pipelines);
  const one = new RegExp(`^${W}/pipelines/([^/]+)(/dry-run|/publish|/runs)?$`).exec(p);
  if (one) {
    const pl = s.pipelines.find((x) => x.id === one[1]);
    if (!pl) return err(404, "not_found", "pipeline not found");
    if (!one[2] && m === "GET") return ok(pl);
    if (one[2] === "/dry-run" && m === "POST") {
      const run = dryRun();
      requestApproval(MAT_APPROVAL, "pipeline_materialize", "postgres:analytics/aos_out.p1_clean", { pipeline_run_id: run.id, candidate: "cand_9f1",
        row_count: 4190, columns: ["sys_id", "priority", "assignment_group", "resolution_hours"], keys: ["sys_id"], previous: "mat_2" }, ["stg_sn.incident"]);
      s.runs = [run, ...s.runs.filter((r) => r.id !== run.id)];
      return ok(run);
    }
  }
  if (m === "GET" && p === `${W}/pipeline-runs`) {
    const pl = url.searchParams.get("pipeline");
    return ok(s.runs.filter((r) => !pl || r.pipeline_id === pl || r.pipeline_name === pl));
  }
  const mz = new RegExp(`^${W}/pipeline-runs/([^/]+)/materialize$`).exec(p);
  if (m === "POST" && mz) {
    if (approvalStatus(String(body.approval_id)) !== "approved") return err(409, "approval_required", "the materialize approval is not approved yet");
    const existing = s.mats.find((x) => x.pipeline_run_id === mz[1]);
    if (existing) return ok(existing);
    const cur = s.mats.find((x) => x.status === "promoted");
    const v = Math.max(...s.mats.map((x) => x.version)) + 1;
    const next: Materialization = { ...mat(`mat_${v}`, v, "promoted", 0, cur?.id ?? null, 4190), pipeline_run_id: mz[1], approval_id: String(body.approval_id) };
    s.mats = [next, ...s.mats.map((x) => (x.id === cur?.id ? { ...x, status: "superseded" } : x))];
    s.runs = s.runs.map((r) => (r.id === mz[1] ? { ...r, status: "succeeded" } : r));
    return ok(next);
  }
  if (m === "GET" && p === `${W}/materializations`) return ok(s.mats);
  const rb = new RegExp(`^${W}/materializations/([^/]+)/rollback$`).exec(p);
  if (m === "POST" && rb) {
    const cur = s.mats.find((x) => x.id === rb[1]);
    if (!cur || cur.status !== "promoted") return err(409, "conflict", `materialization ${rb[1]} is ${cur?.status ?? "missing"}; only the current version rolls back`);
    s.mats = s.mats.map((x) => (x.id === cur.id ? { ...x, status: "rolled_back" } : x.id === cur.previous_id ? { ...x, status: "promoted" } : x));
    return ok(s.mats.find((x) => x.id === cur.previous_id));
  }
  if (m === "GET" && p === `${W}/writer-destinations`) return ok([{ id: "wdst_1", workspace_id: "ws_demo", engine: "postgres:analytics", schema_name: "aos_out", tables: null, status: "active" }]);
  return null;
}
