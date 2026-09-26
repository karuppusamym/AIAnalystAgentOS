/**
 * One status vocabulary -> one colour tone, used by every badge in the UI so a status looks the
 * same on the task board, the investigation tree, approvals, queries and the console.
 */
export type Tone = "success" | "running" | "warning" | "danger" | "neutral" | "info";

const TONES: Record<string, Tone> = {
  // success
  completed: "success", ok: "success", verified: "success", supported: "success", approved: "success",
  executed: "success", succeeded: "success", published: "success", ready: "success", allow: "success",
  final: "success", discovered: "info", validated: "success", resolved: "success",
  // in progress
  running: "running", planning: "running", testing: "running", started: "running", in_progress: "running",
  // needs attention
  waiting_user: "warning", paused: "warning", pending: "warning", inconclusive: "warning", partial: "warning",
  approval_required: "warning", awaiting_approval: "warning", open: "warning", acknowledged: "info", proposed: "info", registered: "info", new: "neutral", approved_plan: "warning",
  // bad
  failed: "danger", rejected: "danger", error: "danger", denied: "danger", deny: "danger", failed_verification: "danger",
  timeout: "danger", invalidated: "danger", alerting: "danger", refused: "danger",
  // inert
  cancelled: "neutral", superseded: "neutral", skipped: "neutral", draft: "neutral", expired: "neutral",
  rolled_back: "neutral", unknown: "neutral", disabled: "neutral",
};

export function toneFor(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  return TONES[status.toLowerCase()] ?? "neutral";
}

/** Alert severity -> tone (critical red, warning amber, info violet-blue). */
export function severityTone(severity: string | null | undefined): Tone {
  const s = (severity ?? "").toLowerCase();
  return s === "critical" ? "danger" : s === "warning" ? "warning" : s === "info" ? "info" : "neutral";
}

const HYPOTHESIS_ICONS: Record<string, string> = {
  supported: "✓",
  rejected: "✗",
  inconclusive: "?",
  testing: "◔",
  superseded: "↷",
  proposed: "○",
  approved: "●",
};

export function hypothesisIcon(status: string): string {
  return HYPOTHESIS_ICONS[status.toLowerCase()] ?? "○";
}

export const TERMINAL_RUN = new Set(["COMPLETED", "FAILED", "REJECTED", "CANCELLED"]);

export const AUTONOMY_LEVELS: { level: number; name: string; description: string }[] = [
  { level: 0, name: "Manual", description: "Agents do nothing on their own; every step is started by a person." },
  { level: 1, name: "Recommends", description: "Agents propose plans, hypotheses and queries; a person executes them." },
  { level: 2, name: "Executes after plan approval", description: "The analysis plan must be approved before agents run it." },
  { level: 3, name: "Executes; publish needs approval", description: "Agents run the analysis end to end; publishing needs an approval." },
  { level: 4, name: "Autonomous within policy", description: "Agents act on their own inside workspace policy. Publication still requires approval in this release." },
];
