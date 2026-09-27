/**
 * The Data Thread in memory (docs/20-contracts/04-brief-steps-api.md §4–7): an investigation's steps
 * on a main branch, edit/re-run with dependents re-running and earlier verdicts turning VOID, old
 * versions kept, pins as hash-bound approvals, forks, compare and merge, and notebooks whose cells
 * are steps. Mirrors the server's rules closely enough for the UI to be tested against them.
 */
import type { Branch, BranchCompare, Notebook, NotebookCell, Step, StepResult, VerificationState } from "../api";

const T = "2026-09-26T10:00:00Z";
export const THREAD_RUN = "run_demo";
export const MAIN = "brn_main";
export const FORK = "brn_whatif";
export const PIN_APPROVAL = "apr_pin";
export const NOTEBOOK = "nb_1";

type Reply = { status: number; body: unknown } | null;
const ok = (body: unknown, status = 200): Reply => ({ status, body });
const err = (status: number, code: string, message: string, details: Record<string, unknown> = {}): Reply =>
  ({ status, body: { error: { code, message, retryable: false, details } } });

export const verified = (id: string): VerificationState => ({
  state: "ACTIVE", badge: "verified", record_id: `ver_${id}`, verdict: "verified", verifier: "selfcheck", fingerprint: `fp_${id}`,
  dependencies: [], created_at: T, void: null, flags: [],
});
const failedVerdict = (id: string): VerificationState => ({ ...verified(id), badge: "failed", verdict: "failed_verification" });
const voided = (v: VerificationState, kind: string, reason: string): VerificationState =>
  ({ ...v, state: "VOID", badge: "void", void: { kind, reason, at: T } });

const check = (name: string, passed = true, detail = "", extra: Partial<Step["checks"][number]> = {}) =>
  ({ check: name, passed, severity: passed ? "info" : "error", detail, evidence: {}, corrected: false, ...extra });
const PASSING = [check("empty_result", true, "3 rows"), check("truncation", true, "complete: 3 of 3 rows"), check("magnitude", true, "within 2x of history")];

interface StepRow { versions: Step[]; results: Record<number, StepResult> }

interface ThreadState {
  steps: Record<string, StepRow>;
  branches: Record<string, Branch>;
  order: Record<string, string[]>; // own steps per branch, in order
  pinApproved: boolean;
  pinRequested: boolean;
  askIngested: boolean;
  notebooks: Record<string, { id: string; title: string; revision: number; branch_id: string; cells: string[] }>;
}

let s: ThreadState; // seeded below, once the fixtures it uses are defined

function mkStep(id: string, branch: string, seq: number, kind: string, title: string, spec: Record<string, unknown>, over: Partial<Step> = {}): Step {
  return {
    schema_version: 1, id, version: 1, current_version: 1, kind, title, status: "ok", spec, spec_hash: `sh_${id}_1`, receipts: [],
    result_snapshot: { kind: "artifact", id: `art_${id}_1`, version: 1, content_hash: `ch_${id}_1`, media_type: "application/json" },
    chart_spec: null, checks: PASSING, corrections: [], verification_record: verified(`${id}_1`), depends_on: [], inputs: {},
    branch_id: branch, container: { type: "run", id: THREAD_RUN }, seq, origin: { type: "ingested", id: `${id}_origin` }, forked_from: null,
    reason: "ingested", error: null, inherited: false, created_by: "usr_admin", created_at: T, ...over,
  };
}

const SQL_COUNT = "SELECT assignment_group, COUNT(*) AS incidents\nFROM stg_sn.incident\nWHERE priority = '1'\nGROUP BY assignment_group\nORDER BY 2 DESC";
export const SQL_MTTR = "SELECT assignment_group, AVG(resolution_hours) AS mttr_hours\nFROM stg_sn.incident\nWHERE priority = '1'\nGROUP BY assignment_group\nORDER BY 2 DESC";

function seed(): ThreadState {
  const st: ThreadState = { steps: {}, branches: {}, order: {}, pinApproved: false, pinRequested: false, askIngested: false, notebooks: {} };
  st.branches[MAIN] = { id: MAIN, name: "main", container: { type: "run", id: THREAD_RUN }, parent_branch_id: null, forked_from: null, base: [],
    status: "open", merged_into: [], created_by: "usr_admin", created_at: T };
  const add = (step: Step, result: StepResult) => {
    st.steps[step.id] = { versions: [step], results: { 1: result } };
    (st.order[step.branch_id] ??= []).push(step.id);
  };
  add(mkStep("stp_plan", MAIN, 1, "plan", "Plan: why are P1 resolution times rising?", { text: "Profile P1 incidents, compare groups, verify." },
    { status: "recorded", verification_record: null, checks: [], result_snapshot: null }), { text: "Profile P1 incidents, compare groups, verify." });
  add(mkStep("stp_count", MAIN, 2, "query", "P1 incidents by assignment group", { sql: SQL_COUNT, source_id: "src_sn" }, {
    receipts: [{ kind: "query", role: "result", query_id: "qry_s1", sql: SQL_COUNT, query_hash: "qh_1", result_hash: "rh_count_1", row_count: 3,
      snapshot: "stg_sn.incident@2026-09-26" }],
    checks: [...PASSING, check("fanout", false, "a join multiplied rows 1.4x; de-duplicated on sys_id", { corrected: true, severity: "warning" })],
    corrections: [{ check: "fanout", reason: "join to sys_user duplicated incidents", from_sql: "… JOIN sys_user …", to_sql: SQL_COUNT, round: 1 }],
  }), { columns: ["assignment_group", "incidents"], rows: [["Network", 182], ["Desktop", 140], ["Database", 96]], row_count: 3, truncated: false });
  add(mkStep("stp_mttr", MAIN, 3, "query", "Mean P1 resolution hours by group", { sql: SQL_MTTR, source_id: "src_sn" }, {
    receipts: [{ kind: "query", role: "result", query_id: "qry_s2", sql: SQL_MTTR, query_hash: "qh_2", result_hash: "rh_mttr_1", row_count: 3,
      snapshot: "stg_sn.incident@2026-09-26" }],
  }), { columns: ["assignment_group", "mttr_hours"], rows: [["Network", 9.4], ["Desktop", 6.1], ["Database", 5.2]], row_count: 3, truncated: false });
  add(mkStep("stp_claim", MAIN, 4, "claim", "Network is the slowest group", { text: "Network resolves P1 incidents in 9.4 hours on average." }, {
    depends_on: ["stp_mttr"], inputs: { stp_mttr: "stv_mttr_1" }, checks: [check("numbers", true, "9.4 binds to stp_mttr row 1")],
  }), { text: "Network resolves P1 incidents in 9.4 hours on average." });
  add(mkStep("stp_chart", MAIN, 5, "chart", "MTTR by group (chart)", { chart: { type: "bar", x: "assignment_group", y: "mttr_hours" } }, {
    depends_on: ["stp_mttr"], inputs: { stp_mttr: "stv_mttr_1" }, chart_spec: { type: "bar", x: "assignment_group", y: "mttr_hours" },
    checks: [check("chart_fields", true, "x and y exist in stp_mttr")],
  }), { columns: ["assignment_group", "mttr_hours"], rows: [["Network", 9.4], ["Desktop", 6.1], ["Database", 5.2]], row_count: 3, truncated: false });
  st.notebooks[NOTEBOOK] = { id: NOTEBOOK, title: "P1 exploration", revision: 1, branch_id: "brn_nb", cells: [] };
  return st;
}

s = seed();

export function resetThread(): void {
  s = seed();
}

/** Approve the pin request, as an approver would in the inbox. */
export function approvePin(): void {
  s.pinApproved = true;
}

const current = (id: string): Step => s.steps[id].versions[s.steps[id].versions.length - 1];

function stepsOf(branch: string): Step[] {
  const b = s.branches[branch];
  const own = (s.order[branch] ?? []).map(current);
  if (!b?.parent_branch_id) return own;
  const parent = stepsOf(b.parent_branch_id);
  const cut = parent.findIndex((x) => x.id === b.forked_from?.step_id);
  return [...parent.slice(0, cut < 0 ? parent.length : cut).map((x) => ({ ...x, inherited: true })), ...own];
}

function branchesOf(container: string): Branch[] {
  return Object.values(s.branches).filter((b) => b.container.id === container);
}

/** Edit or re-run: a new version of the step, its dependents re-run, earlier verdicts VOID. */
function revise(id: string, reason: "edited" | "rerun", edit: { spec?: Record<string, unknown> | null; title?: string | null } = {}) {
  const row = s.steps[id];
  const prev = current(id);
  const v = prev.version + 1;
  const voidedRecords: string[] = [];
  row.versions = row.versions.map((x) => (x.verification_record && x.verification_record.state === "ACTIVE"
    ? (voidedRecords.push(x.verification_record.record_id!), { ...x, verification_record: voided(x.verification_record, reason === "edited" ? "edit" : "rerun",
      reason === "edited" ? `the step was edited to v${v}` : `re-run as v${v}`) }) : x));
  const sql = String(edit.spec?.sql ?? prev.spec.sql ?? "");
  const narrowed = /auto/i.test(sql);
  const next: Step = { ...prev, version: v, current_version: v, spec: edit.spec ?? prev.spec, title: edit.title ?? prev.title, reason,
    spec_hash: `sh_${id}_${v}`, verification_record: verified(`${id}_${v}`), created_at: T,
    receipts: prev.receipts.map((r) => ({ ...r, sql, result_hash: `rh_${id}_${v}` })),
    result_snapshot: prev.result_snapshot ? { ...prev.result_snapshot, id: `art_${id}_${v}`, version: v, content_hash: `ch_${id}_${v}` } : null };
  row.versions = [...row.versions.map((x) => ({ ...x, current_version: v })), next];
  const res = row.results[prev.version];
  row.results[v] = narrowed && res.rows ? { ...res, rows: res.rows.map((r) => [r[0], Math.round((Number(r[1]) * 0.86) * 10) / 10]) } : res;
  const rerun: Step[] = [];
  for (const dep of Object.keys(s.steps).filter((k) => current(k).depends_on.includes(id) && current(k).branch_id === prev.branch_id)) {
    const dRow = s.steps[dep];
    const d = current(dep);
    const dv = d.version + 1;
    if (d.verification_record?.state === "ACTIVE") voidedRecords.push(d.verification_record.record_id!);
    dRow.versions = dRow.versions.map((x) => (x.verification_record?.state === "ACTIVE"
      ? { ...x, verification_record: voided(x.verification_record, "dependency", `${prev.title} changed (v${v})`) } : x));
    const claimBroken = d.kind === "claim" && narrowed;
    const nd: Step = { ...d, version: dv, current_version: dv, reason: "upstream_changed", inputs: { [id]: `stv_${id}_${v}` },
      status: claimBroken ? "flagged" : "ok", created_at: T,
      checks: claimBroken ? [check("numbers", false, `9.4 no longer appears in ${prev.title} v${v}`)] : d.checks,
      verification_record: claimBroken ? failedVerdict(`${dep}_${dv}`) : verified(`${dep}_${dv}`) };
    dRow.versions = [...dRow.versions.map((x) => ({ ...x, current_version: dv })), nd];
    dRow.results[dv] = d.kind === "chart" ? row.results[v] : dRow.results[d.version];
    rerun.push(nd);
  }
  return { step: next, rerun, voided_records: voidedRecords };
}

function fork(branch: string, body: { from_step_id: string; name?: string | null; include_downstream?: boolean }) {
  const parent = s.branches[branch];
  const from = current(body.from_step_id);
  const b: Branch = { id: FORK, name: body.name || "what if", container: parent.container, parent_branch_id: branch,
    forked_from: { step_id: from.id, version: from.version }, base: [], status: "open", merged_into: [], created_by: "usr_admin", created_at: T };
  s.branches[FORK] = b;
  s.order[FORK] = [];
  const all = stepsOf(branch);
  const at = all.findIndex((x) => x.id === from.id);
  const copy = body.include_downstream ? all.slice(at) : [from];
  const idMap: Record<string, string> = {};
  for (const src of copy) idMap[src.id] = `${src.id}_b`;
  for (const src of copy) {
    const nid = idMap[src.id];
    const st: Step = { ...src, id: nid, version: 1, current_version: 1, branch_id: FORK, reason: "forked", inherited: false,
      forked_from: { step_id: src.id, version: src.version }, depends_on: src.depends_on.map((d) => idMap[d] ?? d),
      verification_record: src.verification_record ? verified(`${nid}_1`) : null };
    s.steps[nid] = { versions: [st], results: { 1: s.steps[src.id].results[src.version] } };
    s.order[FORK].push(nid);
  }
  return { branch: b, steps: stepsOf(FORK) };
}

function compare(a: string, b: string): BranchCompare {
  const A = stepsOf(a);
  const B = stepsOf(b);
  const rows: BranchCompare["steps"] = [];
  for (const x of A) {
    const twin = B.find((y) => y.id === x.id) ?? B.find((y) => y.forked_from?.step_id === x.id);
    if (!twin) {
      rows.push({ match: "only_a", root: x.id, a: x, b: null, numbers: null, verdicts: { a: x.verification_record, b: null } });
      continue;
    }
    const ra = s.steps[x.id].results[x.version];
    const rb = s.steps[twin.id].results[twin.version];
    const same = JSON.stringify(ra) === JSON.stringify(rb);
    const ha = typeof ra?.rows?.[0]?.[1] === "number" ? ra.rows[0][1] as number : null;
    const hb = typeof rb?.rows?.[0]?.[1] === "number" ? rb.rows[0][1] as number : null;
    const diverged = x.id !== twin.id && JSON.stringify(x.spec) !== JSON.stringify(twin.spec);
    rows.push({
      match: x.id === twin.id ? "shared" : diverged ? "diverged" : same ? "equivalent" : "diverged", root: x.id, a: x, b: twin,
      spec_diff: diverged ? [{ path: "sql", a: x.spec.sql, b: twin.spec.sql }] : [],
      numbers: { same_result: same, headline: ha !== null && hb !== null ? { a: ha, b: hb, delta: Math.round((hb - ha) * 100) / 100 } : null,
        row_count: { a: ra?.row_count ?? null, b: rb?.row_count ?? null },
        cells: same || !ra?.rows || !rb?.rows ? [] : ra.rows.map((r, i) => ({ key: String(r[0]), column: String(ra.columns?.[1] ?? "value"), a: r[1], b: rb.rows?.[i]?.[1],
          delta: typeof r[1] === "number" && typeof rb.rows?.[i]?.[1] === "number" ? Math.round(((rb.rows[i][1] as number) - r[1]) * 100) / 100 : null })) },
      verdicts: { a: x.verification_record, b: twin.verification_record },
    });
  }
  for (const y of B) if (!rows.some((r) => r.b?.id === y.id)) rows.push({ match: "only_b", root: y.id, a: null, b: y, numbers: null, verdicts: { a: null, b: y.verification_record } });
  return { a: s.branches[a], b: s.branches[b], steps: rows, summary: { diverged: rows.filter((r) => r.match === "diverged").length } };
}

function merge(branch: string, body: { title?: string | null }) {
  const steps = stepsOf(branch);
  const bad = steps.filter((x) => x.verification_record?.state === "VOID");
  if (bad.length) return err(403, "policy_denied", `a step's verdict is VOID: ${bad.map((x) => x.title).join(", ")}; re-run it first`, { steps: bad.map((x) => x.id) });
  const b = s.branches[branch];
  s.branches[branch] = { ...b, status: "merged", merged_into: ["art_thread_report"] };
  const flagged = steps.filter((x) => x.status === "flagged");
  return ok({
    report: { id: "art_thread_report", version: 1, name: body.title || `Data Thread: ${b.name}`, content: { kind: "data_thread", title: body.title || b.name,
      sections: steps.filter((x) => x.status !== "flagged").map((x) => ({ step_id: x.id, version: x.version, title: x.title })),
      branches: [{ id: b.id, name: b.name, parent: b.parent_branch_id }] } },
    branch: s.branches[branch], included: steps.filter((x) => x.status !== "flagged").map((x) => x.id),
    excluded: flagged.map((x) => ({ step_id: x.id, reason: "flagged: failed verification" })),
  });
}

const stepPath = (p: string, W: string) => new RegExp(`^${W}/steps/([^/]+)(/versions|/runs|/pins)?$`).exec(p);

// ------------------------------------------------------------------------------------ notebooks
function nbView(id: string): Notebook {
  const nb = s.notebooks[id];
  return { id, title: nb.title, revision: nb.revision, branch_id: nb.branch_id,
    cells: nb.cells.map((c) => ({ ...current(c), cell: (current(c).origin.cell as string) ?? "sql", source: String(current(c).origin.source ?? "") }) as NotebookCell) };
}

function runCell(id: string, cell: string, source: string, version: number): { step: Step; result: StepResult } {
  const base = mkStep(id, s.notebooks[NOTEBOOK].branch_id, 0, cell === "markdown" ? "claim" : cell === "sql" ? "query" : "method", `Cell`, {},
    { container: { type: "notebook", id: NOTEBOOK }, origin: { cell, source }, reason: version === 1 ? "created" : "edited", version, current_version: version,
      spec_hash: `sh_${id}_${version}`, verification_record: verified(`${id}_${version}`) });
  if (cell === "markdown") return { step: { ...base, status: "recorded", verification_record: null, checks: [] }, result: { text: source } };
  if (cell === "sql") {
    if (/drop|delete|update/i.test(source)) {
      return { step: { ...base, status: "failed", error: "refused by the query gateway: only SELECT statements are allowed", verification_record: null,
        checks: [], result_snapshot: null }, result: {} };
    }
    return { step: { ...base, spec: { sql: source, cell: "sql" }, receipts: [{ kind: "query", query_id: `qry_${id}_${version}`, sql: source, result_hash: `rh_${id}` }] },
      result: { columns: ["assignment_group", "incidents"], rows: [["Network", 182], ["Desktop", 140]], row_count: 2, truncated: false } };
  }
  if (/1\s*\/\s*0/.test(source)) {
    // a runtime error in allowed code runs and fails: the version is recorded as failed (policy refusals are 422s, see threadRoute)
    return { step: { ...base, status: "failed", error: "ZeroDivisionError: division by zero (line 1)", verification_record: null, checks: [],
      result_snapshot: null }, result: {} };
  }
  return { step: { ...base, spec: { cell: "python", code: source } }, result: { columns: ["value"], rows: [[322]], row_count: 1, truncated: false } };
}

/** Route the steps, branches, pins and notebooks API. */
export function threadRoute(m: string, p: string, url: URL, W: string, body: Record<string, unknown>, headers: Record<string, string>): Reply {
  const thread = new RegExp(`^${W}/threads/(run|ask_thread|notebook)/([^/]+)(/ingest)?$`).exec(p);
  if (thread) {
    const [, type, id, ingest] = thread;
    if (ingest && m === "POST") {
      if (type === "ask_thread") s.askIngested = true;
      return ok({ recorded: type === "ask_thread" ? 2 : 0, skipped: type === "run" ? 5 : 0 });
    }
    if (m !== "GET") return null;
    if (type === "run" && id === THREAD_RUN) return ok({ branch: s.branches[MAIN], steps: stepsOf(MAIN), branches: branchesOf(THREAD_RUN) });
    if (type === "ask_thread") {
      const b: Branch = { id: `brn_${id}`, name: "main", container: { type, id }, parent_branch_id: null, forked_from: null, status: "open", merged_into: [] };
      const steps = s.askIngested ? [
        { ...current("stp_count"), id: "stp_ask_1", branch_id: b.id, container: { type, id }, seq: 1, title: "How many P1 incidents per week?" },
      ] : [];
      if (s.askIngested && !s.steps.stp_ask_1) s.steps.stp_ask_1 = { versions: [steps[0]], results: { 1: s.steps.stp_count.results[1] } };
      return ok({ branch: b, steps, branches: [b] });
    }
    return ok({ branch: { id: `brn_${id}`, name: "main", container: { type, id }, parent_branch_id: null, forked_from: null, status: "open", merged_into: [] }, steps: [], branches: [] });
  }
  const br = new RegExp(`^${W}/branches/([^/]+)(/steps|/forks|/compare|/merge)?$`).exec(p);
  if (br) {
    const [, id, action] = br;
    if (!s.branches[id]) return err(404, "not_found", "branch not found");
    if (!action && m === "GET") return ok({ branch: s.branches[id], steps: stepsOf(id) });
    if (action === "/forks" && m === "POST") return ok(fork(id, body as { from_step_id: string }), 201);
    if (action === "/compare" && m === "GET") return ok(compare(id, url.searchParams.get("with") ?? MAIN));
    if (action === "/merge" && m === "POST") return merge(id, body);
    if (action === "/steps" && m === "POST") {
      const nid = `stp_new_${(s.order[id] ?? []).length + 1}`;
      const kind = String(body.kind);
      const st = mkStep(nid, id, (s.order[id] ?? []).length + 1, kind, String(body.title ?? `New ${kind} step`), body.spec as Record<string, unknown>,
        { reason: "created", depends_on: (body.depends_on as string[]) ?? [] });
      s.steps[nid] = { versions: [st], results: { 1: kind === "claim" ? { text: String((body.spec as { text?: string }).text ?? "") }
        : { columns: ["assignment_group", "incidents"], rows: [["Network", 182]], row_count: 1, truncated: false } } };
      (s.order[id] ??= []).push(nid);
      return ok(st, 201);
    }
    return null;
  }
  const sp = stepPath(p, W);
  if (sp) {
    const [, id, action] = sp;
    if (!s.steps[id]) return err(404, "not_found", "step not found");
    if (!action && m === "GET") {
      const v = Number(url.searchParams.get("version") ?? current(id).version);
      const ver = s.steps[id].versions.find((x) => x.version === v) ?? current(id);
      return ok({ ...ver, result: s.steps[id].results[ver.version] ?? {} });
    }
    if (!action && m === "PATCH") {
      const im = headers["if-match"];
      if (!im) return err(428, "precondition_required", "send If-Match");
      if (Number(im.replace(/"/g, "")) !== current(id).version) return err(412, "precondition_failed", `step ${id} is at version ${current(id).version}`);
      return ok(revise(id, "edited", body as { spec?: Record<string, unknown> }));
    }
    if (action === "/versions" && m === "GET") {
      return ok({ step_id: id, current_version: current(id).version, versions: [...s.steps[id].versions].reverse() });
    }
    if (action === "/runs" && m === "POST") return ok(revise(id, "rerun"));
    if (action === "/pins" && m === "POST") {
      const st = current(id);
      if (st.status !== "ok" || st.verification_record?.state !== "ACTIVE") {
        return err(409, "conflict", "only an ok version with an ACTIVE verified record can be pinned");
      }
      if (!body.approval_id) {
        s.pinRequested = true;
        return ok({ status: "approval_required", approval_id: PIN_APPROVAL, payload_hash: "ph_pin_7c1e",
          frozen: { step_id: id, version: st.version, sql: st.spec.sql, target: body.target } }, 202);
      }
      if (!s.pinApproved) return err(409, "approval_not_approved", "the approval is still pending: an approver decides it in the inbox");
      return ok({ status: "pinned", pin: { id: "pin_1", step_id: id, version: st.version, target: body.target, dashboard: body.dashboard ?? null,
        schedule_id: body.target === "schedule" ? "sch_pin_1" : null, frozen_hash: "ph_pin_7c1e" } }, 201);
    }
    return null;
  }
  if (m === "POST" && p === `/approvals/${PIN_APPROVAL}/approve`) {
    s.pinApproved = true;
    return ok({ id: PIN_APPROVAL, status: "approved" });
  }
  // notebooks
  if (p === `${W}/notebooks`) {
    if (m === "GET") return ok(Object.values(s.notebooks).map((n) => ({ id: n.id, title: n.title, revision: n.revision, branch_id: n.branch_id,
      cells: n.cells.length, created_at: T, updated_at: T })));
    if (m === "POST") {
      const id = `nb_${Object.keys(s.notebooks).length + 1}`;
      s.notebooks[id] = { id, title: String(body.title), revision: 1, branch_id: `brn_${id}`, cells: [] };
      return ok({ id, title: body.title, revision: 1, branch_id: `brn_${id}`, cells: 0, created_at: T }, 201);
    }
  }
  const nb = new RegExp(`^${W}/notebooks/([^/]+)(/cells(?:/([^/]+))?|/runs)?$`).exec(p);
  if (nb) {
    const [, id, action, cellId] = nb;
    const book = s.notebooks[id];
    if (!book) return err(404, "not_found", "notebook not found");
    if (!action && m === "GET") return ok(nbView(id));
    if (action === "/cells" && m === "POST") {
      // as the server: the sandbox policy refuses a disallowed import before any cell is created
      const bad = /^\s*(?:import|from)\s+(os|sys|subprocess|socket)\b/m.exec(String(body.source));
      if (body.cell === "python" && bad) {
        return err(422, "invalid_input", `the Python is refused by the sandbox policy: line 1: import of '${bad[1]}' is not allowed`,
          { problems: [`line 1: import of '${bad[1]}' is not allowed`] });
      }
      const cid = `cell_${book.cells.length + 1}`;
      const { step, result } = runCell(cid, String(body.cell), String(body.source), 1);
      const st = { ...step, title: String(body.title ?? `Cell ${book.cells.length + 1}`), seq: book.cells.length + 1 };
      s.steps[cid] = { versions: [st], results: { 1: result } };
      book.cells.push(cid);
      book.revision += 1;
      return ok({ ...st, cell: body.cell, source: body.source }, 201);
    }
    if (action?.startsWith("/cells/") && cellId && m === "PATCH") {
      const im = headers["if-match"];
      if (!im) return err(428, "precondition_required", "send If-Match");
      const cur = current(cellId);
      if (Number(im.replace(/"/g, "")) !== cur.version) return err(412, "precondition_failed", `cell ${cellId} is at version ${cur.version}`);
      const v = cur.version + 1;
      const { step, result } = runCell(cellId, String(cur.origin.cell), String(body.source), v);
      const row = s.steps[cellId];
      row.versions = [...row.versions.map((x) => ({ ...x, current_version: v,
        verification_record: x.verification_record ? voided(x.verification_record, "edit", `the cell was edited to v${v}`) : null })),
      { ...step, title: String(body.title ?? cur.title), seq: cur.seq }];
      row.results[v] = result;
      book.revision += 1;
      const rerun = book.cells.filter((c) => c !== cellId && current(c).origin.cell === "python" && current(c).seq > cur.seq).map((c) => {
        const d = current(c);
        const nd = { ...d, version: d.version + 1, current_version: d.version + 1, reason: "upstream_changed", verification_record: verified(`${c}_${d.version + 1}`) };
        s.steps[c].versions = [...s.steps[c].versions.map((x) => ({ ...x, verification_record: x.verification_record ? voided(x.verification_record, "dependency", "an upstream cell changed") : null })), nd];
        s.steps[c].results[nd.version] = s.steps[c].results[d.version];
        return nd;
      });
      return ok({ step: current(cellId), rerun, voided_records: [`ver_${cellId}_${cur.version}`] });
    }
    if (action === "/runs" && m === "POST") {
      book.revision += 1;
      return ok({ ...nbView(id), executed: [...book.cells] });
    }
  }
  return null;
}

export function threadState() {
  return s;
}
