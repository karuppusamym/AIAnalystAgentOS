/**
 * Investigation board model (P4-U03): hypothesis columns by status and the "Why trust this" facts
 * of a finding, derived from the REV verification record (agents/critic.py). Pure functions.
 *
 * Trust is stated check by check, with the deterministic evidence first (re-run hash, second
 * method, q-value, effect size, n, population) and model opinions last and labelled as such:
 * a model can move confidence, never verify (ADR-0008).
 */
import type { Approval, Hypothesis, Insight, InsightDetail, VerificationCheck } from "../api";
import { fmtNumber, fmtP } from "./format";

export type ColumnId = "proposed" | "testing" | "supported" | "rejected" | "inconclusive";

export const COLUMNS: { id: ColumnId; label: string; hint: string }[] = [
  { id: "proposed", label: "Proposed", hint: "Waiting to be tested" },
  { id: "testing", label: "Testing", hint: "Queries and statistics running" },
  { id: "supported", label: "Supported", hint: "The test supports the hypothesis" },
  { id: "rejected", label: "Rejected", hint: "Not supported by the data, or rejected by a person" },
  { id: "inconclusive", label: "Inconclusive", hint: "Not enough evidence either way" },
];

const STATUS_COLUMN: Record<string, ColumnId> = {
  proposed: "proposed", approved: "proposed", new: "proposed",
  testing: "testing", running: "testing",
  supported: "supported", verified: "supported",
  rejected: "rejected", not_supported: "rejected",
  inconclusive: "inconclusive",
};

/** Column for a hypothesis status; `null` for superseded ones (a replan replaced them). */
export function columnOf(status: string): ColumnId | null {
  const s = status.toLowerCase();
  if (s === "superseded") return null;
  return STATUS_COLUMN[s] ?? "proposed";
}

export interface BoardModel {
  columns: { id: ColumnId; label: string; hint: string; items: { hypothesis: Hypothesis; findings: Insight[] }[] }[];
  superseded: Hypothesis[];
  orphans: Insight[];
}

export function buildBoard(hypotheses: Hypothesis[], insights: Insight[]): BoardModel {
  const byHyp = new Map<string, Insight[]>();
  const ids = new Set(hypotheses.map((h) => h.id));
  const orphans: Insight[] = [];
  for (const i of insights) {
    if (i.hypothesis_id && ids.has(i.hypothesis_id)) byHyp.set(i.hypothesis_id, [...(byHyp.get(i.hypothesis_id) ?? []), i]);
    else orphans.push(i);
  }
  const sorted = [...hypotheses].sort((a, b) => b.priority_score - a.priority_score || a.code.localeCompare(b.code, undefined, { numeric: true }));
  const columns = COLUMNS.map((c) => ({ ...c, items: [] as { hypothesis: Hypothesis; findings: Insight[] }[] }));
  const superseded: Hypothesis[] = [];
  for (const h of sorted) {
    const col = columnOf(h.status);
    if (col === null) superseded.push(h);
    else columns.find((c) => c.id === col)!.items.push({ hypothesis: h, findings: byHyp.get(h.id) ?? [] });
  }
  return { columns, superseded, orphans };
}

export type TrustState = "pass" | "fail" | "unknown" | "info";

export interface TrustFact {
  id: string;
  label: string;
  state: TrustState;
  /** The value in words: "identical result hash", "q = 0.0010". */
  value: string;
  detail?: string;
}

const CHECK_LABEL: Record<string, string> = {
  reproducible_rerun: "Re-run hash match",
  second_method: "Independent second method",
  significance_after_bh: "q-value (Benjamini–Hochberg)",
  effect_size: "Effect size",
  sample_size: "Sample size (n)",
  representative_population: "Representative population",
  method_fit: "Method fits the data",
  no_overreach: "Wording matches the evidence",
};

/** Deterministic checks first, in the order a reviewer asks for them. */
const ORDER = ["reproducible_rerun", "second_method", "significance_after_bh", "effect_size", "sample_size", "representative_population",
  "method_fit", "no_overreach"];

function check(checks: VerificationCheck[], name: string): VerificationCheck | undefined {
  return checks.find((c) => c.check === name);
}

const known = (v: unknown) => v !== null && v !== undefined && v !== "" && !(typeof v === "number" && !Number.isFinite(v));

/**
 * The trust facts of a finding. Unreported evidence is `unknown`, not a pass: a finding whose
 * verification record is empty shows every check as unknown.
 */
export function trustFacts(insight: Insight, hypothesis?: Hypothesis | null, detail?: InsightDetail | null): TrustFact[] {
  const v = insight.verification ?? {};
  const checks = v.evaluate ?? [];
  const r = hypothesis?.result ?? null;
  const facts: TrustFact[] = [];
  for (const name of ORDER) {
    const c = check(checks, name);
    const label = CHECK_LABEL[name];
    const state: TrustState = c ? (c.passed ? "pass" : "fail") : "unknown";
    switch (name) {
      case "reproducible_rerun": {
        const hashes = (detail?.queries ?? []).map((q) => q.result_hash).filter(Boolean) as string[];
        const repro = v.verify?.reproducible;
        const st: TrustState = c ? state : repro === true ? "pass" : repro === false ? "fail" : "unknown";
        facts.push({ id: name, label, state: st,
          value: st === "pass" ? "identical result hash on re-run" : st === "fail" ? "re-run differed or failed" : "not reported",
          detail: [c?.detail, hashes.length ? `result hash ${hashes.map((h) => h.slice(0, 12)).join(", ")}` : ""].filter(Boolean).join(" · ") || undefined });
        break;
      }
      case "second_method": {
        const sm = v.verify?.second_method as Record<string, unknown> | null | undefined;
        const value = sm && known(sm.test)
          ? `${String(sm.test)}: p ${fmtP(sm.p_value)}${known(sm.effect_size) ? `, effect ${fmtNumber(sm.effect_size, 3)}` : ""}`
          : c ? (c.passed ? "agrees" : "does not agree") : "not reported";
        facts.push({ id: name, label, state, value, detail: c?.detail });
        break;
      }
      case "significance_after_bh": {
        const q = r?.p_adjusted ?? null;
        facts.push({ id: name, label, state, value: known(q) ? `q = ${fmtP(q)}` : c ? c.detail : "not reported", detail: c?.detail });
        break;
      }
      case "effect_size": {
        const value = known(r?.effect_size) ? `${r?.effect_label ?? "effect"} = ${fmtNumber(r?.effect_size, 3)}` : c ? c.detail : "not reported";
        facts.push({ id: name, label, state, value, detail: c?.detail });
        break;
      }
      case "sample_size": {
        const n = known(r?.n) ? r?.n : known(insight.population_size) && insight.population_size > 0 ? insight.population_size : null;
        facts.push({ id: name, label, state, value: n !== null && n !== undefined ? `n = ${fmtNumber(n)}` : "not reported", detail: c?.detail });
        break;
      }
      case "representative_population": {
        const method = c?.method ?? null;
        facts.push({ id: name, label, state, value: c ? (c.passed ? `representative${method ? ` (${method})` : ""}` : `not representative${method ? ` (${method})` : ""}`) : "not reported",
          detail: c?.detail });
        break;
      }
      default:
        facts.push({ id: name, label, state, value: c ? (c.passed ? "passed" : "failed") : "not reported", detail: c?.detail });
    }
  }
  for (const c of checks) {
    if (!ORDER.includes(c.check)) facts.push({ id: c.check, label: c.check.replace(/_/g, " "), state: c.passed ? "pass" : "fail", value: c.passed ? "passed" : "failed", detail: c.detail });
  }
  return facts;
}

/** Caveats a reader must see: data quality, population, contradictions and reviewer notes. */
export function caveatsOf(insight: Insight): string[] {
  const out = [...(insight.caveats ?? [])];
  for (const c of insight.verification?.verify?.contradictions ?? []) out.push(`Contrasts with ${c} on the same outcome and segment.`);
  return [...new Set(out)];
}

/** Model opinions recorded by REV. They adjust confidence only; verification is deterministic. */
export function modelOpinions(insight: Insight): { label: string; value: string }[] {
  const v = insight.verification?.verify;
  const out: { label: string; value: string }[] = [];
  if (v?.jev) out.push({ label: `JEV (${v.jev.model})`, value: `p(supports claim) ${fmtNumber(v.jev.p_supports * 100, 0)}%` });
  const im = v?.independent_model;
  if (im?.model && im.review) {
    const supports = (im.review as { supports?: unknown }).supports;
    out.push({ label: `Independent model (${im.model})`, value: supports === true ? "supports the claim" : supports === false ? "does not support the claim" : "reviewed" });
  } else if (im?.unavailable) {
    out.push({ label: "Independent model", value: `unavailable (${im.unavailable})` });
  }
  return out;
}

/** Approvals that gate what this run may publish; shown so trust includes "who signed off". */
export function relevantApprovals(approvals: Approval[]): Approval[] {
  return approvals.filter((a) => a.status !== "invalidated");
}
