import type { FindingStrength, HoldoutCheck, Hypothesis, Insight } from "../api";
import { fmtNumber, fmtP } from "./format";
import { methodLabel } from "./methods";

/**
 * P8-15: what a reader must not skip about a finding, in plain words. A finding is "weak" when it only just
 * clears its method's bars, and it counts as confirmed only when the same test, run once after the claim
 * was locked, held on rows the discovery never read (`evidence/holdout.py`).
 */
export type Badge = { label: string; tone: "success" | "warning" | "neutral"; title: string };

export function strengthBadge(s: FindingStrength | null | undefined): Badge | null {
  if (!s) return null;
  const why = (s.reasons ?? []).join("; ");
  if (s.label === "weak") return { label: "weak evidence", tone: "warning", title: `Only just passes: ${why}` };
  if (s.label === "strong") return { label: "strong evidence", tone: "success", title: why };
  return null; // moderate: nothing to warn about; the numbers are in the expanded row
}

export function holdoutBadge(h: HoldoutCheck | null | undefined, validation?: string | null): Badge | null {
  if (!h && validation !== "confirmed") return null; // older findings, and findings that failed verification
  if (validation === "confirmed" || h?.confirmed) {
    return { label: "confirmed on held-out data", tone: "success", title: h?.partition ? `Held on ${h.partition}` : "Confirmed on separate data" };
  }
  if (h && !h.evaluated) return { label: "not confirmed", tone: "warning", title: `Not checked on held-out rows: ${h.reason ?? "no held-out rows"}` };
  return { label: "not confirmed on held-out data", tone: "warning", title: h?.reason ?? "The held-out rows do not support it" };
}

export function strengthText(s: FindingStrength | null | undefined): string | null {
  if (!s) return null;
  const effect = s.effect !== null && s.effect !== undefined
    ? `${s.effect_label ? methodLabel(s.effect_label) : "effect"} ${fmtNumber(s.effect, 3)}` : null;
  const vs = s.threshold !== null && s.threshold !== undefined && s.margin !== null && s.margin !== undefined
    ? ` vs the minimum ${fmtNumber(s.threshold, 3)} (${fmtNumber(s.margin, 2)}×)` : "";
  const q = s.q !== null && s.q !== undefined ? `; adjusted p ${fmtP(s.q)}` : "";
  return `${s.label[0].toUpperCase()}${s.label.slice(1)}${effect ? `: ${effect}${vs}` : ""}${q}.`;
}

export function holdoutText(h: HoldoutCheck | null | undefined): string | null {
  if (!h) return null;
  if (!h.evaluated) return `Not checked on held-out rows: ${h.reason ?? "no held-out rows"}.`;
  const compared = h.contrast?.length ? ` (${h.contrast.join(" vs ")})` : "";
  const top = h.top ? `; ${h.top} on top${h.claim?.top && h.claim.top !== h.top ? `, not ${String(h.claim.top)}` : ""}` : "";
  const p = h.p_one_sided !== null && h.p_one_sided !== undefined ? `one-sided p ${fmtP(h.p_one_sided)}` : `p ${fmtP(h.p_value)}`;
  const effect = h.effect_size !== null && h.effect_size !== undefined
    ? `, ${h.effect_label ? methodLabel(h.effect_label) : "effect"} ${fmtNumber(h.effect_size, 3)}` : "";
  const verdict = h.confirmed ? "Confirmed" : "Not confirmed";
  return `${verdict} on ${h.partition ?? "held-out rows"}${compared}: ${fmtNumber(h.n)} rows${top}, ${p}${effect}.`
    + (h.reason && !h.confirmed ? ` ${h.reason[0].toUpperCase()}${h.reason.slice(1)}.` : "");
}

/** A hypothesis row's standing: its own result's strength, else its first finding's; its holdout, else its finding's. */
export function hypothesisStanding(h: Hypothesis, findings: Insight[]): { strength: FindingStrength | null; holdout: HoldoutCheck | null; validation: string | null } {
  const f = findings[0];
  return {
    strength: h.result?.strength ?? f?.strength ?? null,
    holdout: h.holdout ?? f?.holdout ?? null,
    validation: f?.validation ?? null,
  };
}
