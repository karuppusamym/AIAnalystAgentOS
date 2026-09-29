import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { HoldoutCheck, Hypothesis, Insight } from "../api";
import { InvestigationBoard } from "../components/InvestigationBoard";
import { holdoutBadge, holdoutText, strengthBadge, strengthText } from "../lib/standing";

// P8-15: "weak" and "confirmed on held-out data" / "not confirmed" read plainly on the hypotheses view.
const T = "2026-09-28T10:00:00Z";
const WEAK = { label: "weak" as const, effect: 0.1175, effect_label: "rank_biserial", threshold: 0.1, margin: 1.175, q: 0.0028, alpha: 0.05,
  reasons: ["the effect (0.117) is 1.2x the minimum that counts (0.1), under 1.5x: it only just passes"] };
const MODERATE = { label: "moderate" as const, effect: 0.0848, effect_label: "cramers_v", threshold: 0.05, margin: 1.7, q: 1.6e-6, alpha: 0.05, reasons: [] };
const HELD: HoldoutCheck = {
  evaluated: true, partition: "30% of rows held out by Order ID", claim: { top: "Marketplace" }, claim_locked_at: T,
  partition_accessed_at: "2026-09-28T10:00:01Z", supported: true, top: "Marketplace", direction: "higher", test: "chi_square_independence",
  p_value: 0.0388, p_one_sided: 0.0194, alpha: 0.05, effect_size: 0.0689, effect_label: "cramers_v", n: 900,
  contrast: ["Marketplace", "Store"], confirmed: true,
};
const NOT_HELD: HoldoutCheck = {
  ...HELD, claim: { top: "Delivered" }, top: "Delivered", supported: false, confirmed: false, p_value: 0.2983, p_one_sided: 0.1492,
  effect_size: 0.0667, effect_label: "rank_biserial", n: 1801, contrast: null,
  reason: "the held-out rows do not show the effect at the required size and significance",
};

const hyp = (id: string, code: string, statement: string, extra: Partial<Hypothesis> = {}): Hypothesis => ({
  id, workspace_id: "ws", run_id: "run", code, question: "Why are orders returned?", statement, spec: {}, priority: "high", priority_score: 0.9,
  status: "supported", methods: ["rate_by_segment"], evidence: [], confidence: null, conclusion: null, parent_id: null, iteration: 1,
  origin: "planner", created_at: T, result: null, experiment_id: null, ...extra,
});
const finding = (id: string, code: string, hid: string, extra: Partial<Insight>): Insight => ({
  id, workspace_id: "ws", run_id: "run", hypothesis_id: hid, code, title: `Finding ${code}`, finding: "text", confidence: 0.8,
  population_size: 4200, business_impact: {}, caveats: [], evidence: [], verified: true, verification: {}, status: "verified",
  narrative_source: "template", created_at: T, ...extra,
});

describe("finding standing (P8-15)", () => {
  it("words strength and the held-out check plainly", () => {
    expect(strengthBadge(WEAK)).toMatchObject({ label: "weak evidence", tone: "warning" });
    expect(strengthBadge(MODERATE)).toBeNull();
    expect(strengthText(WEAK)).toMatch(/^Weak: .* 0\.118 vs the minimum 0\.1 \(1\.18×\); adjusted p 0\.0028\.$/);
    expect(holdoutBadge(HELD, "confirmed")).toMatchObject({ label: "confirmed on held-out data", tone: "success" });
    expect(holdoutBadge(NOT_HELD, "exploratory")).toMatchObject({ label: "not confirmed on held-out data", tone: "warning" });
    expect(holdoutBadge({ evaluated: false, reason: "held-out confirmation is off" }, "exploratory"))
      .toMatchObject({ label: "not confirmed", title: "Not checked on held-out rows: held-out confirmation is off" });
    expect(holdoutBadge(null, "exploratory")).toBeNull();
    expect(holdoutText(HELD)).toMatch(
      /^Confirmed on 30% of rows held out by Order ID \(Marketplace vs Store\): 900 rows; Marketplace on top, one-sided p 0\.0194, .+ 0\.069\.$/);
    expect(holdoutText(NOT_HELD)).toMatch(/^Not confirmed on .*: 1,801 rows; Delivered on top, one-sided p 0\.1492, .* The held-out rows do not show/);
  });

  it("shows the badges on the hypothesis rows and finding cards, and the numbers in the open row", () => {
    const hypotheses = [
      hyp("h8", "H-8", "Returns differ by sales channel", { result: { test: "chi_square_independence", n: 4200, p_value: 2.7e-7, p_adjusted: 1.6e-6,
        effect_size: 0.0848, effect_label: "cramers_v", strength: MODERATE }, holdout: HELD }),
      hyp("h3", "H-3", "Unit price differs by status", { result: { test: "mann_whitney_u", n: 4199, p_value: 7.5e-4, p_adjusted: 0.0015,
        effect_size: 0.139, effect_label: "rank_biserial", strength: WEAK }, holdout: NOT_HELD }),
    ];
    const insights = [
      finding("i8", "I-1", "h8", { validation: "confirmed", strength: MODERATE, holdout: HELD }),
      finding("i3", "I-2", "h3", { validation: "exploratory", strength: WEAK, holdout: NOT_HELD }),
    ];
    render(<MemoryRouter><InvestigationBoard wsId="ws" runId="run" objective="Understand returns" hypotheses={hypotheses} insights={insights}
      approvals={[]} readOnly onChanged={() => undefined} /></MemoryRouter>);
    const h8 = screen.getByRole("article", { name: "Hypothesis H-8" });
    const h3 = screen.getByRole("article", { name: "Hypothesis H-3" });
    const summary8 = h8.querySelector("summary")!.textContent!;
    const summary3 = h3.querySelector("summary")!.textContent!;
    expect(summary8).toMatch(/confirmed on held-out data/);
    expect(summary8).not.toMatch(/weak evidence/);
    expect(summary3).toMatch(/weak evidence/);
    expect(summary3).toMatch(/not confirmed on held-out data/);
    expect(within(within(h3).getByRole("article", { name: "Finding I-2" })).getByText("weak evidence")).toBeTruthy();
    expect(within(within(h8).getByRole("article", { name: "Finding I-1" })).getByText("confirmed on held-out data")).toBeTruthy();
    const firm3 = within(h3).getByRole("list", { name: "How firm H-3 is" }).textContent!;
    expect(firm3).toMatch(/Strength: Weak/);
    expect(firm3).toMatch(/Held-out check: Not confirmed on 30% of rows held out by Order ID: 1,801 rows/);
    const text = document.body.textContent ?? "";
    expect(text.match(/\b(JEV|REV|OKF|Ossie|rungs?|hash(es)?)\b/)?.[0] ?? null).toBeNull();
  });
});
