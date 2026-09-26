/**
 * Ask screen helpers (P4-U02): thread grouping, provenance and staleness pills, one refusal state per
 * kind, and the promote actions. Pure functions, so the rules are unit-tested without rendering.
 */
import type { AskPromotion, AskRefusal, AskStaleness, AskThread, AskTurn, ContextReceipt, DecisionRow } from "../api";
import type { StateKind } from "../components/ui";
import type { Tone } from "./status";

export interface ThreadGroup {
  label: string;
  items: AskThread[];
}

/** Today / Previous 7 days / Earlier, newest activity first; empty groups are dropped. */
export function groupThreads(threads: AskThread[], now: Date = new Date()): ThreadGroup[] {
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const week = day - 7 * 86_400_000;
  const groups: ThreadGroup[] = [{ label: "Today", items: [] }, { label: "Previous 7 days", items: [] }, { label: "Earlier", items: [] }];
  const sorted = [...threads].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  for (const t of sorted) {
    const at = Date.parse(t.updated_at);
    groups[at >= day ? 0 : at >= week ? 1 : 2].items.push(t);
  }
  return groups.filter((g) => g.items.length > 0);
}

export interface Pill {
  tone: Tone;
  label: string;
  title?: string;
}

/** Where the answer came from, in the order a reader checks it: who answered, the gateway, the tables. */
export function provenancePills(turn: AskTurn): Pill[] {
  const p = turn.provenance ?? {};
  const out: Pill[] = [];
  if (turn.answered_by === "registry") {
    const name = p.verified_query?.name ?? (turn.verified_query?.name as string | undefined);
    out.push({ tone: "success", label: `Verified query${name ? `: ${name}` : ""}`, title: "Answered from the verified-query registry, no model call" });
  } else if (turn.answered_by === "model") {
    out.push({ tone: "info", label: `Generated SQL${turn.model ? ` · ${turn.model}` : ""}`,
      title: "A model wrote the SQL; the platform validated and ran it" });
  }
  if (turn.status === "answered") out.push({ tone: "neutral", label: "Validated by the query gateway", title: "Read-only, within your access" });
  if ((p.repairs ?? 0) > 0) out.push({ tone: "warning", label: `${p.repairs} repair${p.repairs === 1 ? "" : "s"}`, title: "The gateway refused earlier SQL" });
  if (p.cache_hit) out.push({ tone: "neutral", label: "Cached result" });
  for (const a of p.assets ?? []) {
    const src = [a.source_name, a.execution_mode].filter(Boolean).join(", ");
    out.push({ tone: "neutral", label: a.business_name ? `${a.business_name} (${a.asset})` : a.asset, title: src || undefined });
  }
  return out;
}

const STALE_TONE: Record<AskStaleness["state"], Tone> = { fresh: "success", aging: "warning", stale: "danger", changed: "warning", unknown: "neutral" };

export function stalenessPill(s: AskStaleness | null | undefined): Pill {
  const state = s?.state ?? "unknown";
  return { tone: STALE_TONE[state] ?? "neutral", label: s?.label ?? "Data freshness unknown", title: s?.data_as_of ? `Oldest table data: ${s.data_as_of}` : undefined };
}

export interface RefusalView {
  state: StateKind;
  action: "parameters" | "rephrase" | "explain" | "sources" | "access" | "retry";
  actionLabel: string;
}

/** One refusal state per kind, each with the action that is its remedy. Unknown kinds read as failures. */
export const REFUSAL_VIEWS: Record<string, RefusalView> = {
  needs_input: { state: "refused", action: "parameters", actionLabel: "Answer with these values" },
  clarify: { state: "refused", action: "rephrase", actionLabel: "Rephrase the question" },
  sql_rejected: { state: "refused", action: "explain", actionLabel: "Explain the SQL" },
  no_model: { state: "refused", action: "explain", actionLabel: "Write the SQL instead" },
  no_scope: { state: "empty", action: "sources", actionLabel: "Go to Sources" },
  policy_denied: { state: "not-entitled", action: "access", actionLabel: "See the workspace policy" },
  budget_exceeded: { state: "refused", action: "retry", actionLabel: "Try again" },
  spend_cap: { state: "refused", action: "explain", actionLabel: "Write the SQL instead" },
  timeout: { state: "failed", action: "rephrase", actionLabel: "Narrow the question" },
  unavailable: { state: "failed", action: "retry", actionLabel: "Try again" },
  failed: { state: "failed", action: "retry", actionLabel: "Try again" },
};

export function refusalView(r: AskRefusal | null | undefined): RefusalView {
  return REFUSAL_VIEWS[r?.kind ?? "failed"] ?? REFUSAL_VIEWS.failed;
}

export const PROMOTE_LABELS: Record<string, string> = {
  verified_query: "Save as verified query",
  metric: "Propose as metric",
  monitor: "Monitor this",
  dashboard: "Add to dashboard",
  investigate: "Investigate why",
};

/** What a promotion did, in words (an approval is pending, not done). */
export function promotionText(p: AskPromotion): string {
  const name = p.name ? ` "${p.name}"` : "";
  switch (p.target) {
    case "verified_query": return `Saved as verified query${name}: the same question now answers without a model.`;
    case "metric": return p.status === "approved" ? `Metric${name} is approved.` : `Metric${name} proposed: an approver approves it in the semantic layer.`;
    case "monitor": return `Monitor${name} created.`;
    case "dashboard": return p.status === "approval_required"
      ? "Waiting for approval before the chart is added to the dashboard." : `Added to the dashboard ${String(p.dashboard ?? "")}.`.trim();
    case "investigate": return "Investigation started.";
    default: return `${p.target}: ${p.status}`;
  }
}

/** Only an answer that returned rows can be promoted; the server checks this again. */
export function canPromote(turn: AskTurn): boolean {
  return turn.status === "answered" && !!turn.result;
}

/** One line per decision for the Decision tab: who decided what, and whether a fallback happened. */
export function decisionLine(d: DecisionRow): string {
  const answer = typeof d.answer === "string" ? d.answer : JSON.stringify(d.answer);
  const by = d.backend === "rules" ? "rule" : d.model ? `${d.backend} (${d.model})` : d.backend;
  return `${d.purpose}: ${answer} — decided by ${by}${d.fallback_reason ? `; fallback: ${d.fallback_reason}` : ""}`;
}

/** The model probabilities of a decision, highest first (JEV / classifier backends). */
export function topProbabilities(d: { probabilities?: Record<string, number> | null }, n = 3): [string, number][] {
  return Object.entries(d.probabilities ?? {}).sort(([, a], [, b]) => b - a).slice(0, n);
}

/** A context receipt (P4-K05/U04): title, where in a pack it is, and whether a person reviewed it. */
export function receiptView(r: ContextReceipt) {
  return {
    title: String(r.title ?? r.name ?? r.id ?? r.kind ?? "context item"),
    where: r.path ? `${r.path}${r.anchor ? `#${r.anchor}` : ""}` : null,
    reviewed: typeof r.source === "string" && r.source.startsWith("review:"),
    curated: r.source === "user",
    untrusted: r.trusted === false,
    section: r.section ?? r.kind ?? null,
    documentId: typeof r.document_id === "string" ? r.document_id : null,
  };
}
