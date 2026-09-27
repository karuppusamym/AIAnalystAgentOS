/**
 * Mock routes for the features merged in b82a69c, shown as panels in the light IA: "Why this
 * number?", void badges, schedule pins and upgrades, versioned definitions, recipes and file ingest,
 * the relationship review queue and the semantic model diff. Typed against the client's shapes.
 */
import type {
  DefinitionDiff, DefinitionPage, Insight, IngestResult, PinStatus, Recipe, RecipeRun, RelationshipCandidate, ScheduleDetail,
  SemanticModelDiff, VerificationState, WhyResponse, WhyRunResponse,
} from "../api";

const T = "2026-09-25T09:00:00Z";

export const VERIFIED_STATE: VerificationState = {
  state: "ACTIVE", badge: "verified", record_id: "ver_1", verdict: "verified", verifier: "rev", fingerprint: "f1", dependencies: [],
  created_at: T, void: null, flags: [],
};

export const VOID_STATE: VerificationState = {
  state: "VOID", badge: "void", record_id: "ver_3", verdict: "verified", verifier: "rev", fingerprint: "f3", dependencies: [], created_at: T,
  void: { kind: "data", reason: "the snapshot of stg_sn.incident changed", at: "2026-09-26T08:00:00Z" }, flags: [],
};

export const INSIGHT_VOID_ID = "ins_void";

export function voidInsight(base: Insight): Insight {
  return {
    ...base, id: INSIGHT_VOID_ID, hypothesis_id: null, code: "F3", title: "Change freezes add a day to P1 fixes",
    finding: "P1 incidents raised during a change freeze took 24.3 hours longer to resolve.", verification_state: VOID_STATE,
    created_at: "2026-09-24T09:00:00Z",
  };
}

function why(id: string, code: string, finding: string, state: VerificationState): WhyResponse {
  const voided = state.badge === "void";
  return {
    subject: { type: "insight", id, code, run_id: "run_demo", title: code, finding, status: "verified" },
    verification_state: state,
    state: voided ? "void" : "ok",
    numbers: [{
      text: voided ? "24.3" : "2.1x", value: voided ? 24.3 : 2.1, unit: voided ? "hours" : "ratio", state: voided ? "void" : "ok",
      links: [
        { link: "fact", state: "ok", reason: null, detail: { fact_id: "fct_1", role: "median_ratio", value: 2.1, unit: "ratio", subject: "Network" } },
        { link: "step", state: "ok", reason: null, detail: { hypothesis_id: "hyp_1", code: "H1", method: "numeric_by_segment", method_version: "1.0.0" } },
        { link: "query_receipt", state: "ok", reason: null, detail: { queries: [{ query_id: "qry_h1", kind: "sql", sql: "SELECT assignment_group, resolution_hours FROM stg_sn.incident", rows: 4210, state: "ok" }] } },
        voided
          ? { link: "data_version", state: "changed", reason: "the snapshot of stg_sn.incident changed", detail: { asset: "stg_sn.incident" } }
          : { link: "data_version", state: "ok", reason: null, detail: { asset: "stg_sn.incident" } },
        { link: "semantic_version", state: "not_applicable", reason: null, detail: { metrics: [] } },
        voided
          ? { link: "verdict", state: "void", reason: "data: the snapshot of stg_sn.incident changed", detail: { record_id: "ver_3", state: "VOID" } }
          : { link: "verdict", state: "ok", reason: null, detail: { record_id: "ver_1", state: "ACTIVE" } },
      ],
    }],
  };
}

const PIN_STATUS: PinStatus = {
  state: "upgrade_available", revision: 1, warnings: [], blocking: [], upgrade_hash: "up_7f3a9c", checked_at: T,
  items: [{
    type: "definition", id: "playbook.investigate", pinned: "1.0.0", current: "1.1.0", state: "newer", reason: "a newer version is published",
    diff: [{ path: "steps.verify.second_method", from: false, to: true }],
  }],
};

const RECIPE: Recipe = {
  id: "rcp_1", workspace_id: "ws_demo", name: "p1_incidents_clean", version: 2, status: "published",
  spec: { name: "p1_incidents_clean", nodes: [] }, spec_hash: "abc123", created_by: "usr_admin", created_at: T, published_at: T,
};

const RECIPE_RUN: RecipeRun = {
  id: "rrn_1", workspace_id: "ws_demo", recipe_id: "rcp_1", recipe_name: "p1_incidents_clean", recipe_version: 2, spec_hash: "abc123",
  mode: "materialize", engine: "sql", status: "succeeded", plan: {}, preflight: [], snapshots: {},
  gates: { clean: { blocked: false, kept_rows: 4180, dropped_rows: 30, warnings: [], gates: [
    { gate: "not_null_resolved_at", type: "not_null", severity: "fail", columns: ["resolved_at"], checked_rows: 4210, failed_rows: 30, status: "passed" }] } },
  outputs: { clean: { source_id: "src_out", kept_rows: 4180, dropped_rows: 30, blocked: false } }, query_ids: [], error: null,
  created_by: "usr_admin", created_at: "2026-09-25T10:00:00Z", finished_at: "2026-09-25T10:00:05Z",
};

const CANDIDATE: RelationshipCandidate = {
  id: "rlc_1", workspace_id: "ws_demo", source_id: "src_sn", from_asset: "stg_sn.incident", from_columns: ["assignment_group"],
  to_asset: "stg_sn.sys_user_group", to_columns: ["sys_id"], cardinality: "many_to_one", containment: 0.98, confidence: 0.9,
  evidence: { rows: 4210, sql: "SELECT …" }, assessment: { outcome: "corroborated", approvable: true, warnings: [] }, origin: "discovered",
  status: "pending", proposed_by: "usr_other", approval_id: "apr_rel", decided_by: null, decided_at: null, reason: null, relationship_name: null,
  measured_at: T, content_hash: "c0ffee", created_at: T,
};

const MODEL_DIFF: SemanticModelDiff = {
  version: 3, status: "proposed", base_version: 2, has_changes: true,
  entries: [{ field: "relationships.incident_group.cardinality", change: "changed", before: "many_to_many", after: "many_to_one" }],
};

const DEFINITIONS: DefinitionPage = {
  items: [
    { id: "def_2", workspace_id: "ws_demo", kind: "saved_analysis", key: "p1_weekly", version: 2, status: "draft", title: "P1 weekly review",
      content_hash: "d2", revision: 1, created_by: "usr_admin", published_by: null, retired_by: null, reason: null, published_at: null, retired_at: null,
      created_at: T, updated_at: T },
    { id: "def_1", workspace_id: "ws_demo", kind: "saved_analysis", key: "p1_weekly", version: 1, status: "published", title: "P1 weekly review",
      content_hash: "d1", revision: 2, created_by: "usr_admin", published_by: "usr_admin", retired_by: null, reason: null, published_at: T, retired_at: null,
      created_at: T, updated_at: T },
  ],
  next_cursor: null,
  builtin: [{ kind: "playbook", key: "playbook.investigate", version: "1.1.0", content_hash: "b1", source: "builtin", status: "published", id: null }],
};

const DEF_DIFF: DefinitionDiff = {
  from: DEFINITIONS.items[0], to: DEFINITIONS.items[1], changes: [{ path: "window_days", from: 7, to: 14 }],
};

/** Mutable state for the actions (reset with the rest of the mock). */
const wave = { upgraded: false, candidateDecided: false };
export function resetWave1(): void {
  wave.upgraded = false;
  wave.candidateDecided = false;
}

type Reply = { status: number; body: unknown } | null;

export function wave1Route(m: string, p: string, ws: string, requestBody?: string | null): Reply {
  const W = `/workspaces/${ws}`;
  const ok = (body: unknown, status = 200) => ({ status, body });
  const whyMatch = /^\/insights\/([^/]+)\/why$/.exec(p);
  if (m === "GET" && whyMatch) {
    const id = decodeURIComponent(whyMatch[1]);
    return id === INSIGHT_VOID_ID
      ? ok(why(id, "F3", "P1 incidents raised during a change freeze took 24.3 hours longer to resolve.", VOID_STATE))
      : ok(why(id, "F1", "Network resolves P1 incidents 2.1x slower than the median group.", VERIFIED_STATE));
  }
  if (m === "GET" && /\/analysis\/[^/]+\/why$/.test(p)) {
    const f1 = why("ins_demo", "F1", "Network resolves P1 incidents 2.1x slower than the median group.", VERIFIED_STATE);
    return ok({ run_id: "run_demo", findings: [f1], numbers: 1, by_state: { ok: 1 } } satisfies WhyRunResponse);
  }
  const sch = new RegExp(`^${W}/schedules/([^/]+)(/upgrade)?$`).exec(p);
  if (sch && m === "GET" && !sch[2]) {
    const status: PinStatus = wave.upgraded ? { ...PIN_STATUS, state: "current", revision: 2, items: [], upgrade_hash: null } : PIN_STATUS;
    return ok({
      id: decodeURIComponent(sch[1]), workspace_id: ws, name: "Weekly re-analysis", kind: "reanalysis", cron: "0 7 * * 1", timezone: "UTC",
      config: {}, enabled: true, owner_id: "usr_admin", next_run_at: "2026-09-28T07:00:00Z", last_run_at: null, created_at: T,
      revision: wave.upgraded ? 2 : 1, pin_status: status,
    } satisfies ScheduleDetail);
  }
  if (sch && m === "POST" && sch[2]) {
    const body = requestBody ? JSON.parse(requestBody) as { upgrade_hash?: string } : {};
    if (body.upgrade_hash !== PIN_STATUS.upgrade_hash) return ok({ error: { code: "conflict", message: "the upgrade changed; review it again", details: {} } }, 409);
    wave.upgraded = true;
    return ok({ schedule_id: decodeURIComponent(sch[1]), revision: 2, pin_revision: 2, status: { ...PIN_STATUS, state: "current", items: [] },
      added: ["playbook.investigate@1.1.0"], removed: ["playbook.investigate@1.0.0"] });
  }
  if (m === "GET" && p === `${W}/definitions`) return ok(DEFINITIONS);
  if (m === "GET" && /\/definitions\/[^/]+\/diff$/.test(p)) return ok(DEF_DIFF);
  const defAction = new RegExp(`^${W}/definitions/([^/]+)/(publish|deprecate|retire)$`).exec(p);
  if (m === "POST" && defAction) {
    const d = DEFINITIONS.items.find((x) => x.id === defAction[1]) ?? DEFINITIONS.items[0];
    return ok({ ...d, status: defAction[2] === "publish" ? "published" : defAction[2] === "deprecate" ? "deprecated" : "retired", revision: d.revision + 1 });
  }
  if (m === "GET" && p === `${W}/recipes`) return ok([RECIPE]);
  if (m === "GET" && p === `${W}/recipe-runs`) return ok([RECIPE_RUN]);
  if (m === "POST" && p === `${W}/recipes/${RECIPE.id}/runs`) {
    const body = requestBody ? JSON.parse(requestBody) as { mode?: string } : {};
    return body.mode === "preview"
      ? ok({ ...RECIPE_RUN, id: "rrn_p", mode: "preview", outputs: {}, preview: { clean: { columns: ["number", "assignment_group"],
        rows: [["INC001", "Network"], ["INC002", "Service Desk"]], row_count: 2, truncated: false, dropped_rows: 0, would_block: false } } })
      : ok({ ...RECIPE_RUN, id: "rrn_2" });
  }
  if (m === "POST" && p === `${W}/uploads`) return ok({ path: `/data/uploads/${ws}/orders.csv`, bytes: 120 });
  if (m === "POST" && /\/sources\/[^/]+\/ingest$/.test(p)) {
    const body = requestBody ? JSON.parse(requestBody) as { table?: string; mode?: string } : {};
    return ok({ asset: `stg_files.${body.table ?? "t"}`, asset_id: "ast_f", mode: body.mode ?? "replace", format: "csv" } satisfies IngestResult);
  }
  if (m === "GET" && p === `${W}/semantic/relationships/candidates`) return ok(wave.candidateDecided ? [] : [CANDIDATE]);
  if (m === "POST" && /\/semantic\/relationships\/candidates\/[^/]+\/(accept|reject)$/.test(p)) {
    wave.candidateDecided = true;
    return ok({ ...CANDIDATE, status: p.endsWith("accept") ? "accepted" : "rejected" });
  }
  if (m === "GET" && p === `${W}/semantic/model/diff`) return ok(MODEL_DIFF);
  if (m === "GET" && p === `${W}/semantic/model/suggestion`) return ok(MODEL_SUGGESTION);
  if (m === "POST" && p === `${W}/semantic/model/suggestion/validate`) {
    return ok({ tables: [{ asset_id: "ast_inc", rows: 4210, distinct_keys: 4210, unique: true }],
      joins: [{ from: "stg_sn.incident", to: "stg_sn.sys_user_group", from_columns: ["assignment_group"], to_columns: ["sys_id"],
        rows_before: 4210, rows_after: 4210, fans_out: false }], queries: 3, skipped: [] });
  }
  if (m === "POST" && p === `${W}/semantic/model/suggestion/propose`) return ok({ model_version: 3, status: "proposed", candidates_queued: 1 });
  if (m === "POST" && p === `${W}/semantic/relationships/discover`) return ok([CANDIDATE]);
  return null;
}

/** GET …/semantic/model/suggestion: one fact, one dimension, a pending join, a gap and a candidate metric. */
export const MODEL_SUGGESTION = {
  generated_at: "2026-09-27T12:00:00Z",
  tables: [
    { asset_id: "ast_inc", fq: "stg_sn.incident", name: "incident", business_name: "Incidents", role: "fact", entity: "incident", grain: "one row per incident",
      primary_key: { columns: ["sys_id"], evidence: "declared", unique: null }, time_column: "opened_at", measures: ["reassignment_count"],
      dimensions: ["priority", "category"], confidence: 0.86, issues: [] },
    { asset_id: "ast_grp", fq: "stg_sn.sys_user_group", name: "sys_user_group", business_name: "Group", role: "dimension", entity: "group", grain: null,
      primary_key: { columns: [], evidence: "none", unique: null }, time_column: null, measures: [], dimensions: ["name"], confidence: 0.7,
      issues: [{ code: "no_primary_key", message: "Group has no key: joins to it cannot be checked for duplicates." }] },
  ],
  relationships: [{ from: { asset_id: "ast_inc", fq: "stg_sn.incident", columns: ["assignment_group"] },
    to: { asset_id: "ast_grp", fq: "stg_sn.sys_user_group", columns: ["sys_id"] }, cardinality: "many_to_one", status: "pending", confidence: 0.93,
    evidence: {}, candidate_id: "rlc_1" }],
  metrics: [{ name: "incident_count", label: "Incident count", expression: "COUNT(*)", table_fq: "stg_sn.incident", reason: "one row per incident" }],
  star_schemas: [{ fact: "ast_inc", dimensions: ["ast_grp"] }],
  issues: [{ code: "no_primary_key", message: "Group has no key: joins to it cannot be checked for duplicates.", asset_id: "ast_grp" }],
  summary: { tables: 2, facts: 1, dimensions: 1, relationships_validated: 0, relationships_pending: 1, keys_measured: 0 },
};
