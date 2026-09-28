/**
 * Overview → "What you can do with this data": the actionable patterns of the selected tables with their evidence and next
 * step; "Yes, this" / "Not this" post the person's answer; "Look for more" asks the model and reports what code verified.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { session, type DataShape } from "../api";
import { actionableRows, patternSummary } from "../components/DataShape";
import { bodyOf, calls, mockFetch, renderAt, type Handler } from "./harness";
import { resetMockState, USER, WS } from "./mockBackend";

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

const SHAPE: DataShape = {
  version: "data-shape/1", generated_at: "2026-09-27T10:00:00",
  workspace: [{ kind: "star", label: "Star schema (analytics style)", reasons: ["1 fact table(s) joined to 2 dimension(s)"], tables: [] }],
  summary: { event_log: 1, time_series: 1, ml_candidate: 1, fact: 1 },
  tables: [
    { asset_id: "ast_orders", fq: "s.orders", name: "orders", business_name: "Orders", role: "fact", row_count: 300, unexplained: false, patterns: [
      { kind: "time_series", confidence: 0.8, reasons: ["a time column (ordered_at) and a measure (amount)"], origin: "rules", state: "suggested",
        detail: { time_column: "ordered_at", measures: ["amount"] }, next_step: { action: "investigation", label: "Investigate the trend" } },
      { kind: "ml_candidate", confidence: 0.7, reasons: ["300 rows, 4 usable columns, 2 column(s) worth predicting"], origin: "rules", state: "suggested",
        detail: { targets: [{ column: "late", kind: "classification", why: "true in 25% of rows" }, { column: "amount", kind: "regression", why: "x" }] },
        next_step: { action: "experiment", label: "Try a prediction" } },
      { kind: "fact", confidence: 0.75, reasons: ["role fact"], origin: "rules", state: "suggested", detail: {}, next_step: { action: "ask", label: "Ask" } }] },
    { asset_id: "ast_log", fq: "s.task_activity", name: "task_activity", business_name: null, role: "event", row_count: 400, unexplained: false, patterns: [
      { kind: "event_log", confidence: 0.9, reasons: ["a case, an activity and a time column"], origin: "model", state: "proposed",
        detail: { mapping: { case_column: "task_ref", activity_column: "activity", timestamp_column: "activity_at" } },
        next_step: { action: "process_analysis", label: "Analyse the process" } }] },
  ],
};

function shapeApi(): Handler {
  return (method, path) => {
    if (method === "GET" && path === `/api/workspaces/${WS}/data-shape`) return { status: 200, body: SHAPE };
    if (method === "POST" && path.endsWith("/data-shape/mark")) return { status: 200, body: {} };
    if (method === "POST" && path.endsWith("/data-shape/propose")) {
      return { status: 200, body: { called: true, considered: 2, proposed: 1, rejected: [{ table: "orders", why: "x" }] } };
    }
    return null;
  };
}

describe("Overview → data shape", () => {
  it("lists the actionable patterns with evidence and next steps, and hides plain roles", async () => {
    mockFetch(shapeApi());
    renderAt(`/w/${WS}`);
    const card = await screen.findByRole("region", { name: "What you can do with this data" });
    expect((await within(card).findByTestId("shape-workspace")).textContent).toMatch(/Star schema \(analytics style\)/);
    const list = within(card).getByRole("list", { name: "Detected patterns" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("Orders");
    expect(rows[0].textContent).toContain("amount over ordered_at");
    expect(rows[1].textContent).toContain("late (classification), amount (regression)");
    expect(rows[2].textContent).toContain("suggested by a model, checked in code");
    expect(within(rows[2]).getByRole("link", { name: "Analyse the process" }).getAttribute("href")).toBe(`/w/${WS}/work?tab=process`);
    expect(within(rows[1]).getByRole("link", { name: "Try a prediction" }).getAttribute("href")).toBe(`/w/${WS}/work?tab=experiments`);
  });

  it("posts a person's answer about a pattern", async () => {
    const f = mockFetch(shapeApi());
    renderAt(`/w/${WS}`);
    const card = await screen.findByRole("region", { name: "What you can do with this data" });
    fireEvent.click(await within(card).findByRole("button", { name: "Confirm Event log for task_activity" }));
    const not = within(card).getByRole("button", { name: "Not a Prediction candidate: orders" }) as HTMLButtonElement;
    await waitFor(() => expect(not.disabled).toBe(false));  // one answer at a time
    fireEvent.click(not);
    await waitFor(() => expect(calls(f, "POST", /\/data-shape\/mark$/)).toHaveLength(2));
    const bodies = calls(f, "POST", /\/data-shape\/mark$/).map((c) => bodyOf(c[1]));
    expect(bodies).toContainEqual({ asset_id: "ast_log", kind: "event_log", decision: "confirm" });
    expect(bodies).toContainEqual({ asset_id: "ast_orders", kind: "ml_candidate", decision: "dismiss" });
  });

  it("asks the model for more and says what was verified", async () => {
    mockFetch(shapeApi());
    renderAt(`/w/${WS}`);
    const card = await screen.findByRole("region", { name: "What you can do with this data" });
    fireEvent.click(await within(card).findByRole("button", { name: "Look for more" }));
    expect(await within(card).findByText(/Looked at 2 table\(s\): 1 suggestion\(s\) verified, 1 rejected/)).toBeTruthy();
  });

  it("summarises a pattern in words", () => {
    expect(actionableRows(SHAPE.tables).map((r) => r.pattern.kind)).toEqual(["time_series", "ml_candidate", "event_log"]);
    expect(patternSummary(SHAPE.tables[1].patterns[0])).toBe("case task_ref, step activity, time activity_at");
    expect(patternSummary(SHAPE.tables[0].patterns[2])).toBe("");
  });
});
