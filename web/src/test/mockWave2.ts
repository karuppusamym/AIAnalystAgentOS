/**
 * Mock routes for the wave-2 backends (docs/20-contracts/04-brief-steps-api.md, docs/30-runbooks/ml-api.md,
 * docs/30-runbooks/05-pipelines.md): Start-work job kinds, the brief and readiness, the Data Thread
 * (steps, versions, branches, pins, merge), notebooks, governed ML and pipelines. Typed against the
 * client's shapes; state is reset per test (`resetWave2`).
 */
import type {
  Assertion, JobKindAvailability, ReadinessAssessment, WorkspaceBrief,
} from "../api";
import { decide, resetApprovals } from "./mockApprovals";
import { mlRoute, resetMl } from "./mockMl";
import { resetThread, threadRoute } from "./mockThread";

const T = "2026-09-26T09:00:00Z";

type Reply = { status: number; body: unknown } | null;
const ok = (body: unknown, status = 200): Reply => ({ status, body });
const err = (status: number, code: string, message: string, details: Record<string, unknown> = {}): Reply =>
  ({ status, body: { error: { code, message, retryable: false, details } } });

// ------------------------------------------------------------------------------------ job kinds (P4-04)
const jk = (key: JobKindAvailability["key"], label: string, entry: JobKindAvailability["entry"], extra: Partial<JobKindAvailability> = {}): JobKindAvailability => ({
  key, label, work_order_kind: key, available: true, reasons: [], entry, readiness_checks: ["capability", "scope", "schema_drift"], min_role: "analyst",
  capabilities: [{ id: `playbook.${key}`, ref: `playbook.${key}@1.0.0`, kind: "Playbook", usable: true, reason: null, certification: "tested" }],
  ...extra,
});

export const JOB_KINDS: JobKindAvailability[] = [
  jk("explain", "Explain", { type: "work_order", payload_type: "analysis", route: "/api/workspaces/ws_demo/work-orders" },
    { work_order_kind: "diagnose", capabilities: [{ id: "playbook.investigate", ref: "playbook.investigate@1.0.0", kind: "Playbook", usable: true, reason: null, certification: "certified" }] }),
  jk("compare", "Compare", { type: "work_order", payload_type: "analysis", route: "/api/workspaces/ws_demo/work-orders" }),
  jk("forecast", "Forecast", { type: "work_order", payload_type: "ml", route: "/api/workspaces/ws_demo/work-orders" }, {
    available: false,
    capabilities: [{ id: "method.ml.forecast", ref: "method.ml.forecast@1.0.0", kind: "Method", usable: false, reason: "it is turned off in this workspace", certification: "tested" }],
    reasons: [{ code: "capability_unusable", message: "method.ml.forecast is turned off in this workspace",
      remediation: "A workspace owner enables it (Settings > Capabilities), or an administrator installs the missing profile or extra." }],
  }),
  jk("predict", "Predict", { type: "work_order", payload_type: "ml", route: "/api/workspaces/ws_demo/work-orders" },
    { readiness_checks: ["capability", "scope", "schema_drift", "grain", "key_uniqueness", "label_availability", "coverage", "missingness"] }),
  jk("prepare", "Prepare data", { type: "recipe", route: "/api/workspaces/ws_demo/recipes" }, { min_role: "editor", capabilities: [] }),
  jk("monitor", "Monitor", { type: "monitor", route: "/api/workspaces/ws_demo/monitors" }, { min_role: "editor", capabilities: [] }),
];

// ------------------------------------------------------------------------------------ brief and readiness (P4-04)
const assertion = (a: Partial<Assertion> & Pick<Assertion, "key" | "group" | "field" | "value" | "origin" | "review_state">): Assertion => ({
  subject: null, evidence: [], version: 1, confidence: null, note: null, updated_by: "system", updated_at: T, ...a,
});

function initialBrief(ws: string): WorkspaceBrief {
  return {
    schema_version: 1, workspace_id: ws, version: 3, content_hash: "brief_c3", created_by: "usr_admin", created_at: T, reason: "suggestions",
    assertions: [
      assertion({ key: "data_semantics.grain:stg_sn.incident", group: "data_semantics", field: "grain", subject: "stg_sn.incident",
        value: "one row per incident", origin: "rule", review_state: "suggested", confidence: 0.8,
        evidence: [{ kind: "profile", ref: "stg_sn.incident.sys_id", detail: { distinct_ratio: 1 } }] }),
      assertion({ key: "data_semantics.entity_key:stg_sn.incident", group: "data_semantics", field: "entity_key", subject: "stg_sn.incident",
        value: "sys_id", origin: "source", review_state: "validated", evidence: [{ kind: "check", ref: "key_uniqueness:stg_sn.incident.sys_id" }] }),
      assertion({ key: "time_measures.unit:stg_sn.incident.resolution_hours", group: "time_measures", field: "unit",
        subject: "stg_sn.incident.resolution_hours", value: "hours", origin: "user", review_state: "reviewed", updated_by: "usr_admin" }),
      assertion({ key: "domain.alias:P1", group: "domain", field: "alias", subject: "P1", value: "critical priority", origin: "user",
        review_state: "reviewed" }),
      assertion({ key: "ml_objective.target:stg_sn.incident", group: "ml_objective", field: "target", subject: "stg_sn.incident",
        value: "breached_sla", origin: "model", review_state: "suggested", confidence: 0.55,
        evidence: [{ kind: "column", ref: "stg_sn.incident.breached_sla" }] }),
    ],
  };
}

export function readinessFor(ws: string, jobKind: string, briefVersion: number): ReadinessAssessment {
  const base = { id: `rdy_${jobKind}`, workspace_id: ws, job_kind: jobKind, brief_version: briefVersion, inputs: { assets: ["stg_sn.incident"] },
    inputs_hash: "in_1", alternatives: [] as ReadinessAssessment["alternatives"], created_at: T };
  const pass = (check: string, reason: string, required = true) => ({ check, status: "pass", required, reason });
  if (jobKind === "predict" || jobKind === "forecast") {
    return {
      ...base, status: "blocked", checks: [
        pass("capability", "playbook.train is registered and usable"), pass("scope", "stg_sn.incident is in your scope"),
        { check: "label_availability", status: "fail", required: true, subject: "stg_sn.incident.breached_sla",
          reason: "the label breached_sla is known only after resolution, later than the prediction moment",
          remediation: "State when the label becomes known (Data > Brief: Prediction target), or choose a descriptive task." },
        { check: "freshness", status: "warn", required: false, reason: "stg_sn.incident was staged 30 hours ago", remediation: "Refresh the source." },
      ],
      alternatives: [{ job_kind: "explain", requires_explicit_choice: true, note: "describe what drives SLA breaches instead" }],
    };
  }
  return {
    ...base, status: "ready", checks: [
      pass("capability", "playbook.investigate is registered and usable"), pass("scope", "stg_sn.incident is in your scope"),
      pass("schema_drift", "no column changed since the last crawl"), pass("grain", "one row per incident (validated key sys_id)"),
      { check: "freshness", status: "warn", required: false, reason: "stg_sn.incident was staged 30 hours ago", remediation: "Refresh the source." },
    ],
  };
}

interface Wave2State {
  brief: WorkspaceBrief;
  briefHistory: WorkspaceBrief[];
}

let state: Wave2State = fresh("ws_demo");

function fresh(ws: string): Wave2State {
  const brief = initialBrief(ws);
  return { brief, briefHistory: [brief] };
}

export function resetWave2(): void {
  state = fresh("ws_demo");
  resetThread();
  resetMl();
  resetApprovals();
}

/** Simulate a concurrent editor: the brief moves on under the person looking at it. */
export function bumpBriefElsewhere(): void {
  const b = state.brief;
  state.brief = { ...b, version: b.version + 1, reason: "edited elsewhere", assertions: b.assertions.map((a) =>
    (a.key === "domain.alias:P1" ? { ...a, value: "priority one", version: a.version + 1, updated_by: "usr_other" } : a)) };
  state.briefHistory = [state.brief, ...state.briefHistory];
}

type BriefOp = { op: "set" | "review" | "reject" | "remove"; key?: string; assertion?: { group: string; field: string; subject?: string | null; value: unknown; note?: string | null } };

function patchBrief(body: { ops: BriefOp[]; reason?: string }, ifMatch: string | null): Reply {
  if (!ifMatch) return err(428, "precondition_required", "send If-Match with the resource's ETag (its revision) to edit it");
  if (Number(ifMatch.replace(/"/g, "")) !== state.brief.version) {
    return err(412, "precondition_failed", `the brief is at version ${state.brief.version}, not ${ifMatch}`);
  }
  const changed: string[] = [];
  let assertions = [...state.brief.assertions];
  for (const op of body.ops) {
    if (op.op === "set" && op.assertion) {
      const a = op.assertion;
      const key = `${a.group}.${a.field}${a.subject ? `:${a.subject}` : ""}`;
      assertions = assertions.filter((x) => x.key !== key);
      assertions.push(assertion({ key, group: a.group, field: a.field, subject: a.subject ?? null, value: a.value, origin: "user",
        review_state: "reviewed", note: a.note ?? null, updated_by: "usr_admin" }));
      changed.push(key);
    } else if (op.key) {
      if (op.op === "remove") assertions = assertions.filter((x) => x.key !== op.key);
      else assertions = assertions.map((x) => (x.key === op.key
        ? { ...x, review_state: op.op === "review" ? "reviewed" : "rejected", version: x.version + 1, updated_by: "usr_admin" } : x));
      changed.push(op.key);
    }
  }
  state.brief = { ...state.brief, version: state.brief.version + 1, assertions, reason: body.reason ?? null, created_at: T };
  state.briefHistory = [state.brief, ...state.briefHistory];
  return ok({ ...state.brief, changed, impact: { readiness_assessments_outdated: 1 } });
}

/** Route one wave-2 request (`p` is the path after /api); null when it is not a wave-2 route. */
export function wave2Route(m: string, p: string, url: URL, ws: string, body: string | null | undefined, headers: Record<string, string> = {}): Reply {
  const W = `/workspaces/${ws}`;
  const json = () => (body ? JSON.parse(body) as Record<string, unknown> : {});
  if (m === "GET" && p === `${W}/capabilities`) return ok({ workspace_id: ws, digest: "d1e2f3a4", job_kinds: JOB_KINDS });
  if (p === `${W}/brief`) {
    if (m === "GET") {
      const v = url.searchParams.get("version");
      if (v) return ok(state.briefHistory.find((b) => b.version === Number(v)) ?? state.brief);
      return ok(state.brief);
    }
    if (m === "PATCH") return patchBrief(json() as { ops: BriefOp[] }, headers["if-match"] ?? null);
  }
  if (m === "GET" && p === `${W}/brief/versions`) {
    return ok(state.briefHistory.map((b) => ({ version: b.version, content_hash: b.content_hash, reason: b.reason, created_by: b.created_by,
      created_at: b.created_at, assertions: b.assertions.length })));
  }
  if (m === "POST" && p === `${W}/brief/suggestions`) {
    const had = state.brief.assertions.some((a) => a.key === "time_measures.event_time:stg_sn.incident");
    if (!had) {
      state.brief = { ...state.brief, version: state.brief.version + 1, reason: "suggestions", assertions: [...state.brief.assertions,
        assertion({ key: "time_measures.event_time:stg_sn.incident", group: "time_measures", field: "event_time", subject: "stg_sn.incident",
          value: "opened_at", origin: "rule", review_state: "suggested", evidence: [{ kind: "column", ref: "stg_sn.incident.opened_at" }] })] };
      state.briefHistory = [state.brief, ...state.briefHistory];
    }
    return ok({ ...state.brief, added: had ? [] : ["time_measures.event_time:stg_sn.incident"], updated: [], new_version: !had });
  }
  if (m === "POST" && p === `${W}/readiness`) {
    const b = json() as { job_kind?: string };
    return ok(readinessFor(ws, String(b.job_kind ?? "explain"), state.brief.version), 201);
  }
  if (m === "POST" && p === `${W}/query`) return ok(filteredQuery(String(json().sql ?? "")));
  const decided = /^\/approvals\/([^/]+)\/(approve|reject)$/.exec(p);
  if (m === "POST" && decided) {
    const a = decide(decided[1], decided[2] as "approve" | "reject", (json().reason as string | undefined) ?? null);
    if (a) return ok(a);
  }
  return mlRoute(m, p, url, W, json()) ?? threadRoute(m, p, url, W, json(), headers);
}

/** The gateway's answer to a preview filter: the base rows re-read with the WHERE clause applied. */
export function filteredQuery(sql: string) {
  const mttr = /mttr_hours/.test(sql);
  const columns = mttr ? ["assignment_group", "mttr_hours"] : ["assignment_group", "incidents"];
  let rows: unknown[][] = mttr ? [["Network", 9.4], ["Desktop", 6.1], ["Database", 5.2]] : [["Network", 182], ["Desktop", 140], ["Database", 96]];
  for (const [, col, op, raw] of sql.matchAll(/"(\w+)"\s*(=|!=|>=|<=|>|<)\s*('(?:[^']|'')*'|[\d.]+)/g)) {
    const i = columns.indexOf(col);
    if (i < 0) continue;
    const v = raw.startsWith("'") ? raw.slice(1, -1).replace(/''/g, "'") : Number(raw);
    rows = rows.filter((r) => {
      const x = r[i] as string | number;
      return op === "=" ? x === v : op === "!=" ? x !== v : op === ">" ? x > v : op === ">=" ? x >= v : op === "<" ? x < v : x <= v;
    });
  }
  return { query_id: "qry_filter_1", columns, rows, row_count: rows.length, truncated: false, result_hash: `fh${rows.length}c0ffee0123456789`, sql };
}
