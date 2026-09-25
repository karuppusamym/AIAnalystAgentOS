import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { session } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { buildInFlight, buildSteps, diffLines, EMPTY_KPI, fmtBytes, kpiBody, localKpiProblems, problemsByField } from "../lib/build";
import { SCREEN_BUDGET, SCREENS, to } from "../routes";
import { BUILD_APPROVAL, BUILD_NEW, BUILD_PREV, mockBackend, resetMockState, USER, WS } from "./mockBackend";

vi.setConfig({ testTimeout: 20000 });

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend((init?.method ?? "GET").toUpperCase(), String(input), typeof init?.body === "string" ? init.body : null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function calls(f: ReturnType<typeof mockFetch>, method: string, re: RegExp): [string, RequestInit][] {
  return (f.mock.calls as [string, RequestInit | undefined][])
    .filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u))).map(([u, i]) => [String(u), i ?? {}]);
}

function renderAt(path: string) {
  return render(<AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter></AuthProvider>);
}

async function axeClean(container: HTMLElement) {
  const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`);
}

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

// ------------------------------------------------------------------------------------ pure helpers
describe("build helpers", () => {
  it("reads a job as four steps, with the approval decided in the inbox", () => {
    const states = (status: string, a: string | null) => buildSteps({ status, error: null }, a).map((s) => s.state);
    expect(states("awaiting_approval", "pending")).toEqual(["done", "current", "todo", "todo"]);
    expect(states("awaiting_approval", "approved")).toEqual(["done", "done", "current", "todo"]);
    expect(states("awaiting_approval", "expired")).toEqual(["done", "failed", "todo", "todo"]);
    expect(states("running", "executed")).toEqual(["done", "done", "current", "todo"]);
    expect(states("succeeded", "executed")).toEqual(["done", "done", "done", "done"]);
    expect(states("refused", "invalidated")).toEqual(["done", "done", "failed", "failed"]);
    expect(buildSteps({ status: "failed", error: "dbt exited 1" }, "executed")[3].detail).toBe("dbt exited 1");
  });

  it("polls only while a job is moving", () => {
    expect(buildInFlight({ status: "running" })).toBe(true);
    expect(buildInFlight({ status: "awaiting_approval" }, "pending")).toBe(false);
    expect(buildInFlight({ status: "awaiting_approval" }, "approved")).toBe(true);
    expect(buildInFlight({ status: "succeeded" }, "executed")).toBe(false);
  });

  it("formats rough sizes and keeps unknown unknown", () => {
    expect(fmtBytes(336800)).toBe("336.8 kB");
    expect(fmtBytes(9_600_000)).toBe("9.6 MB");
    expect(fmtBytes(512)).toBe("512 B");
    expect(fmtBytes(null)).toBeNull();
  });

  it("classifies unified diff lines", () => {
    expect(diffLines("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n same").map((l) => l.kind)).toEqual(["meta", "meta", "hunk", "del", "add", "ctx"]);
    expect(diffLines("")).toEqual([]);
  });

  it("checks a KPI draft locally and builds the API body", () => {
    expect(problemsByField(localKpiProblems(EMPTY_KPI))).toEqual({ name: "A name is required.", expression: "An expression is required." });
    expect(problemsByField(localKpiProblems({ ...EMPTY_KPI, name: "p1 rate", expression: "COUNT(*)" })).name).toMatch(/letters, digits/);
    expect(localKpiProblems({ ...EMPTY_KPI, name: "p1_rate", expression: "COUNT(*)" })).toEqual([]);
    expect(kpiBody({ ...EMPTY_KPI, name: " p1_rate ", expression: "COUNT(*)", dimensions: "group, priority,", format: "percent" })).toEqual({
      name: "p1_rate", expression: "COUNT(*)", display_name: null, description: null, format: "percent", grain: null,
      dimensions: ["group", "priority"], filters: [],
    });
  });

  it("stays inside the screen budget: Build tabs are not new screens", () => {
    expect(SCREENS.length).toBeLessThanOrEqual(SCREEN_BUDGET);
    expect(SCREENS.filter((s) => s.journey === "build").map((s) => s.id)).toEqual(["studio", "reports"]);
    expect(to.build(WS, "builds", { job: BUILD_NEW })).toBe(`/w/${WS}/build/studio?tab=builds&job=${BUILD_NEW}`);
  });
});

// ------------------------------------------------------------------------------------ builds
describe("Build studio: dbt builds", () => {
  it("shows the diff against the previous job, the dry run, the estimate and the approval state", async () => {
    mockFetch();
    const { container } = renderAt(to.build(WS, "builds", { job: BUILD_PREV }));
    await screen.findByText(/No earlier build of this target/);
    expect(screen.getByRole("tab", { name: "dbt builds", selected: true })).toBeTruthy();
    expect(screen.getByLabelText("Estimate").textContent).toMatch(/4,210.*rows/);
    expect(screen.getByText("336.8 kB")).toBeTruthy();
    expect(screen.getByText("fails: dropped")).toBeTruthy();
    expect(screen.getByText(/reopen_rate/).closest("details")).toBeTruthy();
    expect(within(screen.getByRole("list", { name: "Build status" })).getAllByText("✓")).toHaveLength(4);
    expect(await axeClean(container)).toEqual([]);
  });

  it("plans a build, selects the new job and sends the approver to the inbox", async () => {
    const f = mockFetch();
    renderAt(to.build(WS, "builds"));
    const form = await screen.findByRole("form", { name: "Plan a build" });
    await waitFor(() => expect(within(form).getByLabelText("From run").querySelectorAll("option").length).toBe(1));
    fireEvent.click(within(form).getByRole("button", { name: "Plan build" }));
    await screen.findByText(/Compared with job/);
    const [, init] = calls(f, "POST", /\/builds$/)[0];
    expect(JSON.parse(String(init.body))).toEqual({ from_run_id: "run_demo", target_schema: "aos_mart" });
    expect(screen.getByText(/2 modified/)).toBeTruthy();
    expect(screen.getByLabelText("Diff of models/p1_incidents.sql").textContent).toContain("+where priority = '1'");
    expect(screen.getByText(/Unchanged: dbt_project.yml/)).toBeTruthy();
    const steps = screen.getByRole("list", { name: "Build status" });
    expect(within(steps).getByText("Approval").closest("li")?.getAttribute("aria-current")).toBe("step");
    expect(screen.getByRole("link", { name: "Review in the approvals inbox" }).getAttribute("href")).toBe(to.approvals(WS));
    // nothing on this screen decides the approval
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    expect(calls(f, "POST", /\/approvals\//)).toEqual([]);
  });

  it("shows the job as succeeded once the approval is decided", async () => {
    mockFetch();
    mockBackend("POST", `/api/workspaces/${WS}/builds`, JSON.stringify({ from_run_id: "run_demo", target_schema: "aos_mart" }));
    mockBackend("POST", `/api/approvals/${BUILD_APPROVAL}/approve`, "{}");
    renderAt(to.build(WS, "builds", { job: BUILD_NEW }));
    await screen.findByText(/Compared with job/);
    expect(screen.getByRole("heading", { name: "Result" })).toBeTruthy();
    expect(screen.getByText("success: 2")).toBeTruthy();
  });
});

// ------------------------------------------------------------------------------------ KPIs
describe("Build studio: KPI editor", () => {
  it("validates live, shows field problems and conflicts, and proposes", async () => {
    const f = mockFetch();
    const { container } = renderAt(to.build(WS, "kpis"));
    const form = await screen.findByRole("form", { name: "Propose a KPI" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "p1 count" } });
    expect(await within(form).findByText(/letters, digits and underscores/)).toBeTruthy();
    expect(within(form).getByLabelText("Name").getAttribute("aria-invalid")).toBe("true");

    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "p1_count" } });
    fireEvent.change(within(form).getByLabelText("Expression"), { target: { value: "priority" } });
    expect(await within(form).findByText("not an aggregate expression", {}, { timeout: 3000 })).toBeTruthy();

    fireEvent.change(within(form).getByLabelText("Expression"), { target: { value: "AVG(resolution_hours)" } });
    expect(await within(form).findByRole("list", { name: "Conflicts" }, { timeout: 3000 })).toBeTruthy();
    expect(within(form).getByText(/mttr_hours already compute this expression/)).toBeTruthy();

    fireEvent.change(within(form).getByLabelText("Expression"), { target: { value: "COUNT(*)" } });
    await within(form).findByText(/well-formed aggregate/, {}, { timeout: 3000 });
    fireEvent.click(within(form).getByRole("button", { name: "Propose KPI" }));
    await within(form).findByText(/waits for an approver who is not you/);
    expect(calls(f, "POST", /\/semantic\/metrics$/)).toHaveLength(1);
    // selecting the new KPI shows the separation-of-duties note instead of approve buttons
    expect(await screen.findByText(/You proposed this version/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Approve v1/ })).toBeNull();
    expect(await axeClean(container)).toEqual([]);
  });

  it("approves another person's proposal through the semantic approve route", async () => {
    const f = mockFetch();
    const { container } = renderAt(to.build(WS, "kpis", { kpi: "mttr_hours" }));
    expect(await screen.findByText(/2 competing definitions/)).toBeTruthy();
    fireEvent.change(await screen.findByLabelText("Reason for mttr_hours v2"), { target: { value: "matches the ops definition" } });
    expect(await axeClean(container)).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Approve v2" }));
    await waitFor(() => expect(calls(f, "POST", /\/semantic\/metrics\/mttr_hours\/approve$/)).toHaveLength(1));
    const [, init] = calls(f, "POST", /\/approve$/)[0];
    expect(JSON.parse(String(init.body))).toEqual({ version: 2, reason: "matches the ops definition" });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve v2" })).toBeNull());
  });
});

// ------------------------------------------------------------------------------------ dashboards
describe("Build studio: dashboards", () => {
  it("previews natively and requests publication as a hash-bound proposal", async () => {
    const f = mockFetch();
    const { container } = renderAt(to.build(WS, "dashboards", { dashboard: "art_dash" }));
    expect(await screen.findByLabelText("Dashboard preview")).toBeTruthy();
    expect(screen.getByText("P1 MTTR by assignment group")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Request publication" }));
    expect(await screen.findByRole("link", { name: "Decide in the approvals inbox" })).toBeTruthy();
    expect(screen.getByText("apr_1")).toBeTruthy();
    expect(calls(f, "POST", /\/artifacts\/art_dash\/publish$/)).toHaveLength(1);
    expect(calls(f, "POST", /\/approvals\//)).toEqual([]);
    expect(await axeClean(container)).toEqual([]);
  });
});
