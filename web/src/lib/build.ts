/** Build journey helpers (P4-U05). Pure functions, unit-tested. */
import type { BuildJob, MetricProblem, MetricProposal } from "../api";

export type StepState = "done" | "current" | "todo" | "failed";

export interface BuildStep {
  key: "planned" | "approval" | "build" | "result";
  label: string;
  state: StepState;
  detail: string;
}

const TERMINAL_APPROVAL_BLOCK = new Set(["rejected", "expired", "invalidated"]);

/**
 * Where a build job stands, as four steps a person can read: dry-run planned → approval → dbt
 * build → result. The job status says what the gateway did; the approval status says what a person
 * decided, which the job only learns when the run resumes (so "approved, waiting to run" is shown).
 */
export function buildSteps(job: Pick<BuildJob, "status" | "error">, approvalStatus: string | null | undefined): BuildStep[] {
  const s = job.status;
  const a = approvalStatus ?? null;
  const planned: BuildStep = { key: "planned", label: "Dry run planned", state: "done", detail: "dbt parse and gateway probe passed" };
  const approval: BuildStep = { key: "approval", label: "Approval", state: "todo", detail: "not requested" };
  const build: BuildStep = { key: "build", label: "dbt build", state: "todo", detail: "writes nothing until approved" };
  const result: BuildStep = { key: "result", label: "Result", state: "todo", detail: "" };

  if (s === "planned") {
    planned.state = "current";
    planned.detail = "planning";
  } else if (s === "awaiting_approval") {
    if (a && TERMINAL_APPROVAL_BLOCK.has(a)) {
      approval.state = "failed";
      approval.detail = `${a}: plan the build again to request a new approval`;
    } else if (a === "approved" || a === "executed") {
      approval.state = "done";
      approval.detail = "approved";
      build.state = "current";
      build.detail = "approved: waiting for the run to resume";
    } else {
      approval.state = "current";
      approval.detail = "waiting for an approver in the inbox";
    }
  } else if (s === "running") {
    approval.state = "done";
    approval.detail = "approved";
    build.state = "current";
    build.detail = "running as the workspace build role";
  } else if (s === "succeeded") {
    approval.state = build.state = result.state = "done";
    approval.detail = "approved and consumed";
    build.detail = "completed";
    result.detail = "tables built";
  } else if (s === "failed" || s === "refused") {
    approval.state = a === "pending" ? "current" : "done";
    approval.detail = a ?? "unknown";
    build.state = "failed";
    build.detail = s === "refused" ? "refused by the build gateway" : "dbt build failed";
    result.state = "failed";
    result.detail = job.error ?? s;
  }
  return [planned, approval, build, result];
}

/** A build job is still moving (worth polling). */
export function buildInFlight(job: Pick<BuildJob, "status">, approvalStatus?: string | null): boolean {
  if (job.status === "running" || job.status === "planned") return true;
  return job.status === "awaiting_approval" && (approvalStatus === "approved" || approvalStatus === "executed");
}

/** Bytes for a rough size estimate; null stays unknown. */
export function fmtBytes(v: number | null | undefined): string | null {
  if (v === null || v === undefined || !Number.isFinite(v)) return null;
  const units = ["B", "kB", "MB", "GB", "TB"];
  let n = v;
  let i = 0;
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000;
    i += 1;
  }
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
}

export type DiffLineKind = "add" | "del" | "hunk" | "meta" | "ctx";

/** Classify unified-diff lines for display (the server computes the diff; this only colours it). */
export function diffLines(diff: string): { kind: DiffLineKind; text: string }[] {
  if (!diff) return [];
  return diff.split("\n").map((text) => {
    if (text.startsWith("+++") || text.startsWith("---")) return { kind: "meta" as const, text };
    if (text.startsWith("@@")) return { kind: "hunk" as const, text };
    if (text.startsWith("+")) return { kind: "add" as const, text };
    if (text.startsWith("-")) return { kind: "del" as const, text };
    return { kind: "ctx" as const, text };
  });
}

/** The KPI editor's draft, as typed (lists as comma-separated text). */
export interface KpiDraft {
  name: string;
  display_name: string;
  expression: string;
  format: string;
  grain: string;
  description: string;
  dimensions: string;
  filters: string;
}

export const EMPTY_KPI: KpiDraft = { name: "", display_name: "", expression: "", format: "", grain: "", description: "", dimensions: "", filters: "" };

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function list(text: string): string[] {
  return text.split(",").map((x) => x.trim()).filter(Boolean);
}

/** The API body for a draft (empty optional fields are omitted, lists split on commas). */
export function kpiBody(d: KpiDraft): MetricProposal {
  return {
    name: d.name.trim(), expression: d.expression.trim(),
    display_name: d.display_name.trim() || null, description: d.description.trim() || null,
    format: (d.format || null) as MetricProposal["format"], grain: d.grain.trim() || null,
    dimensions: list(d.dimensions), filters: list(d.filters),
  };
}

/**
 * What can be said without asking the server (required fields, the name pattern). The server's
 * validation (aggregate shape, subqueries, conflicts) is authoritative and shown next to these.
 */
export function localKpiProblems(d: KpiDraft): MetricProblem[] {
  const out: MetricProblem[] = [];
  const name = d.name.trim();
  if (!name) out.push({ field: "name", message: "A name is required." });
  else if (!NAME_RE.test(name) || name.length > 120) {
    out.push({ field: "name", message: "Use letters, digits and underscores, starting with a letter or underscore (max 120)." });
  }
  if (!d.expression.trim()) out.push({ field: "expression", message: "An expression is required." });
  return out;
}

/** Problems grouped by field, first message wins (one message per field is easier to act on). */
export function problemsByField(problems: MetricProblem[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of problems) if (!(p.field in out)) out[p.field] = p.message;
  return out;
}
