/**
 * Ask's step-by-step (analyst) mode: the mode switch sends `mode: "analyst"`, and an analyst answer
 * shows the cited synthesis, the steps with their computed facts and checks, drivers, a stale notice
 * after a step re-run, and follow-ups.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { session, type AskAnalysis, type AskTurn } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { AnalystAnswer } from "../components/AnalystAnswer";
import { askTurn, headersOf, mockBackend, USER, WS } from "./mockBackend";

const ANALYSIS: AskAnalysis = {
  mode: "analyst",
  plan: { approach: "The two periods, then what drove the change.", origin: "rules", assumptions: ["the latest month vs the one before"],
    steps: [{ n: 1, goal: "Incidents this month vs last month", question: "incidents last month vs this month" },
      { n: 2, goal: "What drove the change, by category", question: "incidents by category last month vs this month" }] },
  steps: [
    { n: 1, kind: "comparison", goal: "Incidents this month vs last month", question: "incidents last month vs this month", status: "answered",
      answered_by: "rules", governance: "ad_hoc", sql: "SELECT 1", result: { query_id: "qry_a", columns: ["period", "incidents"],
        rows: [["2026-08", 800], ["2026-09", 1000]], row_count: 2, truncated: false } as never,
      facts: { statements: ["incidents rose from 800 to 1,000 (+25%)."] },
      comparison: { previous: 800, current: 1000, change: 200, pct_change: 0.25, previous_period: "2026-08", current_period: "2026-09" },
      checks: [{ code: "empty_result", status: "pass", note: "2 rows" }] },
    { n: 2, kind: "drivers", goal: "What drove the change, by category", question: "incidents by category last month vs this month",
      status: "answered", answered_by: "rules", sql: "SELECT 2",
      drivers: { members: 3, drivers: [{ member: "network", previous: 100, current: 260, change: 160, pct_change: 1.6, share_of_change: 0.8 }],
        offsets: [{ member: "email", previous: 200, current: 180, change: -20, pct_change: -0.1, share_of_change: -0.1 }], appeared: ["storage"] },
      checks: [{ code: "missing_periods", status: "suspect", note: "One category has no rows last month" }] },
  ],
  synthesis: { text: "Incidents rose 25% (step 1).", answer: "Incidents rose 25%, from 800 to 1,000 (step 1).",
    evidence: [{ step: 2, text: "Network drove 80% of the change (step 2)." }], caveats: ["Compared the latest month with the one before (step 1)."],
    citations: [1, 2], origin: "template" },
  follow_ups: ["incidents by category over time"],
  headline_step: 1,
};

const TURN: AskTurn = askTurn("askt_an", "Why did incidents increase?", { route: "analyst", answered_by: "rules", analysis: ANALYSIS });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
  try { window.localStorage.clear(); } catch { /* ignore */ }
});

describe("an analyst answer", () => {
  it("leads with the cited answer, then the steps with facts, checks and drivers", () => {
    const onAsk = vi.fn();
    render(<AnalystAnswer turn={TURN} busy={false} onRerunStep={vi.fn()} onResynthesize={vi.fn()} onAsk={onAsk} />);
    const answer = screen.getByRole("region", { name: "Answer" });
    expect(within(answer).getByText(/Incidents rose 25%, from 800 to 1,000/)).toBeTruthy();
    expect(within(answer).getAllByRole("link", { name: "step 1" }).length).toBeGreaterThan(0);
    expect(within(answer).getByRole("link", { name: "step 2" })).toBeTruthy();
    expect(within(answer).getByText(/no model wrote it/)).toBeTruthy();
    expect(screen.getByText("incidents rose from 800 to 1,000 (+25%).")).toBeTruthy();
    expect(screen.getByText("1 to check")).toBeTruthy();
    expect(screen.getByText("One category has no rows last month")).toBeTruthy();
    expect(screen.getByRole("table", { name: /What moved the total/ })).toBeTruthy();
    expect(screen.getByText("drove it")).toBeTruthy();
    expect(screen.getByText(/New: storage/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "incidents by category over time" }));
    expect(onAsk).toHaveBeenCalledWith("incidents by category over time");
  });

  it("marks a stale synthesis and rewrites it; a step re-runs with edited SQL", () => {
    const onResynthesize = vi.fn();
    const onRerunStep = vi.fn();
    const stale = { ...TURN, analysis: { ...ANALYSIS, synthesis: { ...ANALYSIS.synthesis, stale: true, stale_steps: [1] } } };
    render(<AnalystAnswer turn={stale} busy={false} onRerunStep={onRerunStep} onResynthesize={onResynthesize} onAsk={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Rewrite the answer" }));
    expect(onResynthesize).toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "Edit SQL and re-run" })[0]);
    const form = screen.getByRole("form", { name: "Edit the SQL of step 1" });
    fireEvent.change(within(form).getByLabelText("SQL"), { target: { value: "SELECT 3" } });
    fireEvent.click(within(form).getByRole("button", { name: "Run" }));
    expect(onRerunStep).toHaveBeenCalledWith(1, "SELECT 3");
  });
});

describe("the Ask mode switch", () => {
  beforeEach(() => session.set("mock-token", USER));

  it("sends mode analyst when Step by step is chosen, and remembers the choice", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const r = mockBackend(init?.method ?? "GET", String(input), typeof init?.body === "string" ? init.body : null, headersOf(init));
      return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
    });
    render(<AuthProvider><MemoryRouter initialEntries={[`/w/${WS}/ask`]}><AppRoutes /></MemoryRouter></AuthProvider>);
    fireEvent.click(await screen.findByRole("radio", { name: "Step by step" }));
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Why did incidents increase?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => {
      const post = (f.mock.calls as [string, RequestInit | undefined][]).find(([u, i]) => i?.method === "POST" && /\/turns$/.test(String(u)));
      expect(post).toBeTruthy();
      expect(JSON.parse(String(post![1]!.body)).mode).toBe("analyst");
    });
    expect(window.localStorage.getItem("analystos.ask.mode")).toBe("analyst");
  });
});
