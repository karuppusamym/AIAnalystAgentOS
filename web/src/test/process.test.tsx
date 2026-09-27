/**
 * Work → Process (process and task mining): the detected event log pre-fills the mapping, Analyze posts the
 * chosen columns and segment, and the analysis reads in plain words: the process map (as its transition table
 * where there is no canvas), the most common paths with the expected one marked, where cases wait, rework,
 * cancellations, conformance and handovers. A saved analysis opens in Outputs.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session, type ProcessAnalysis, type ProcessCandidate } from "../api";
import { fmtHours, mapLayers } from "../components/ProcessMining";
import { CAPABILITIES } from "../lib/guide";
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

const CANDIDATE: ProcessCandidate = {
  asset_id: "ast_act", asset: "stg_sn.u_task_activity", name: "u_task_activity", business_name: "Task Activity", row_count: 89435,
  role: "event", declared_by: "pack.itsm@1.0.0",
  mapping: { case_column: "task_sys_id", activity_column: "activity", timestamp_column: "activity_at", resource_column: "assignment_group" },
  segments: [{ column: "task_type", values: [
    { value: "incident", count: 63944, label: "Incidents", reference_path: ["Created", "Assigned", "Work started", "Resolved", "Closed"] },
    { value: "change_request", count: 11065, label: "Change requests", reference_path: null }] }],
  score: 20, reasons: ["declared as an event log by pack.itsm@1.0.0"],
  columns: ["activity", "activity_at", "assigned_to", "assignment_group", "sequence", "state_after", "task_number", "task_sys_id", "task_type"],
};

const REF = ["Created", "Assessed", "Authorized", "Scheduled", "Closed"];
const ANALYSIS: ProcessAnalysis = {
  version: "process-mining/1", title: "Process analysis · Task Activity · change_request",
  asset: { id: "ast_act", fq: "stg_sn.u_task_activity", name: "u_task_activity", business_name: "Task Activity" },
  mapping: { case_column: "task_sys_id", activity_column: "activity", timestamp_column: "activity_at", resource_column: null },
  filters: [{ column: "task_type", op: "=", value: "change_request" }], segment: "change_request",
  summary: { cases: 100, events: 480, activities: 6, variants: 3, mean_events_per_case: 4.8, start: "2026-03-01T00:00:00",
    end: "2026-08-30T00:00:00", median_hours: 60, p90_hours: 250, rework_share: 0, cancelled_share: 0.05, fitness: 0.9, handover_share: 0 },
  highlights: ["100 cases and 480 events follow 3 different paths; the most common path covers 85% of cases."],
  activities: REF.map((a) => ({ activity: a, events: 95, cases: 95, starts: a === "Created" ? 100 : 0, ends: a === "Closed" ? 90 : 0 }))
    .concat([{ activity: "Cancelled", events: 5, cases: 5, starts: 0, ends: 5 }]),
  edges: [
    { source: "Created", target: "Assessed", count: 100, cases: 100, median_hours: 13.6, p90_hours: 60 },
    { source: "Assessed", target: "Authorized", count: 85, cases: 85, median_hours: 11, p90_hours: 50 },
    { source: "Assessed", target: "Scheduled", count: 10, cases: 10, median_hours: 20, p90_hours: 70 },
    { source: "Authorized", target: "Scheduled", count: 85, cases: 85, median_hours: 11.3, p90_hours: 58 },
    { source: "Scheduled", target: "Closed", count: 90, cases: 90, median_hours: 30, p90_hours: 90 },
    { source: "Assessed", target: "Cancelled", count: 5, cases: 5, median_hours: 4, p90_hours: 9 },
  ],
  variants: { total: 3, other_cases: 0, other_share: 0, happy_path_rank: 1, top: [
    { rank: 1, activities: REF, steps: 5, cases: 85, share: 0.85, median_hours: 58, happy_path: true },
    { rank: 2, activities: ["Created", "Assessed", "Scheduled", "Closed"], steps: 4, cases: 10, share: 0.1, median_hours: 40, happy_path: false },
    { rank: 3, activities: ["Created", "Assessed", "Cancelled"], steps: 3, cases: 5, share: 0.05, median_hours: 20, happy_path: false }] },
  throughput: { cases: 100, median_hours: 60, p90_hours: 250, mean_hours: 80,
    histogram: [{ label: "< 1 h", from_hours: 0, to_hours: 1, cases: 0 }, { label: "1-3 days", from_hours: 24, to_hours: 72, cases: 70 },
      { label: "> 4 weeks", from_hours: 720, to_hours: null, cases: 30 }],
    by_end_activity: [{ activity: "Closed", cases: 95, median_hours: 62, p90_hours: 250 }] },
  bottlenecks: [{ source: "Scheduled", target: "Closed", count: 90, cases: 90, median_hours: 30, p90_hours: 90, weight_hours: 2700,
    sentence: "Scheduled → Closed takes a median 30.0 h (1 in 10 over 3.8 days) for 90 cases." }],
  rework: { cases: 0, share: 0, activities: [] },
  cancellations: { activities: ["Cancelled"], cases: 5, share: 0.05, median_hours_to_cancel: 20,
    after: [{ activity: "Assessed", cases: 5, share: 1 }], by_resource: [] },
  conformance: { reference: REF, source: "pack.itsm@1.0.0 reference model for change_request", completed_cases: 95, conforming_cases: 85,
    fitness: 0.8947, excluded: { open: 0, cancelled: 5 }, deviating_variants: [],
    deviations: [{ kind: "missing", activity: "Authorized", cases: 10, share: 0.1053, sentence: "'Authorized' skipped in 10 completed cases (11%)." }] },
  handovers: { cases_with_handover: 0, share: 0, pairs: [], ping_pong: [], resources: [] },
  provenance: { queries: ["qry_1"], sql: 'SELECT "task_sys_id" FROM "stg_sn"."u_task_activity"', dialect: "postgres",
    computed_at: "2026-09-27T10:00:00", method: "ordered events read through the query gateway",
    coverage: { events_read: 480, pages: 1, page_rows: 50000, max_events: 250000, truncated: false, split_case: false, note: "every case" } },
  artifact: { id: "art_pm1", name: "Process analysis · Task Activity · change_request", version: 1 },
};

function processApi(candidates: ProcessCandidate[] = [CANDIDATE]): Handler {
  return (method, path) => {
    if (method === "GET" && path.endsWith("/process/candidates")) return { status: 200, body: { version: "process-mining/1", candidates } };
    if (method === "GET" && path.endsWith("/process/analyses")) return { status: 200, body: [] };
    if (method === "POST" && path.endsWith("/process/analyze")) return { status: 200, body: ANALYSIS };
    return null;
  };
}

describe("Work → Process", () => {
  it("pre-fills the detected mapping and posts the chosen columns and segment", async () => {
    const f = mockFetch(processApi());
    renderAt(`/w/${WS}/work?tab=process`);
    const form = await screen.findByRole("form", { name: "Process analysis mapping" });
    expect((within(form).getByLabelText("Case") as HTMLSelectElement).value).toBe("task_sys_id");
    expect((within(form).getByLabelText("Activity") as HTMLSelectElement).value).toBe("activity");
    expect((within(form).getByLabelText("Time") as HTMLSelectElement).value).toBe("activity_at");
    expect((within(form).getByLabelText("Resource (optional)") as HTMLSelectElement).value).toBe("assignment_group");
    expect((within(form).getByLabelText("Segment") as HTMLSelectElement).value).toBe("incident");
    expect(within(form).getByText(/Mapping declared by pack.itsm@1.0.0/)).toBeTruthy();

    fireEvent.change(within(form).getByLabelText("Segment"), { target: { value: "change_request" } });
    fireEvent.change(within(form).getByLabelText("Resource (optional)"), { target: { value: "" } });
    fireEvent.click(within(form).getByRole("button", { name: "Analyze" }));
    const result = await screen.findByRole("region", { name: "Process analysis" });
    expect(bodyOf(calls(f, "POST", /\/process\/analyze$/)[0][1])).toEqual({
      asset_id: "ast_act", case_column: "task_sys_id", activity_column: "activity", timestamp_column: "activity_at",
      resource_column: null, filters: [{ column: "task_type", op: "=", value: "change_request" }], save: true });

    expect(within(result).getByText(/the most common path covers 85% of cases/)).toBeTruthy();
    const map = within(result).getByRole("region", { name: "Process map" });
    const transitions = within(map).getByRole("table");
    expect(within(transitions).getByText("Created").closest("tr")!.textContent).toContain("13.6 h");
    const paths = within(result).getByRole("region", { name: "Most common paths" });
    const first = within(paths).getAllByRole("row")[1];
    expect(first.textContent).toContain("Created → Assessed → Authorized → Scheduled → Closed");
    expect(within(first).getByText("expected path")).toBeTruthy();
    expect(within(within(result).getByRole("region", { name: "Where cases wait" }))
      .getByText("Scheduled → Closed takes a median 30.0 h (1 in 10 over 3.8 days) for 90 cases.")).toBeTruthy();
    expect(within(within(result).getByRole("region", { name: "Conformance" }))
      .getByText("'Authorized' skipped in 10 completed cases (11%).")).toBeTruthy();
    expect(within(result).getByRole("region", { name: "Cancellations" }).textContent).toMatch(/after ‘Assessed’: 5 \(100%\)/);
    expect(within(result).getByRole("region", { name: "Handovers" }).textContent).toMatch(/Choose a resource column/);
    expect(screen.getByRole("link", { name: ANALYSIS.artifact!.name }).getAttribute("href")).toBe(`/w/${WS}/outputs?artifact=art_pm1`);
  });

  it("explains what to do when no event log is selected", async () => {
    mockFetch(processApi([]));
    renderAt(`/w/${WS}/work?tab=process`);
    expect(await screen.findByText("No event log found")).toBeTruthy();
  });

  it("hides fewer transitions on the map when asked", async () => {
    mockFetch(processApi());
    renderAt(`/w/${WS}/work?tab=process`);
    fireEvent.click(within(await screen.findByRole("form", { name: "Process analysis mapping" })).getByRole("button", { name: "Analyze" }));
    const map = await screen.findByRole("region", { name: "Process map" });
    expect(within(map).getAllByRole("row")).toHaveLength(1 + 6);
    fireEvent.change(within(map).getByLabelText("Transitions shown"), { target: { value: "0.1" } });
    expect(within(map).getAllByRole("row")).toHaveLength(1 + 5);
  });
});

describe("process map helpers", () => {
  it("layers the main flow top to bottom and leaves loops as back edges", () => {
    const depth = mapLayers(["Created", "Assigned", "Resolved", "Reopened", "Closed"], [
      { source: "Created", target: "Assigned", count: 10, cases: 10, median_hours: 1, p90_hours: 1 },
      { source: "Assigned", target: "Resolved", count: 10, cases: 10, median_hours: 1, p90_hours: 1 },
      { source: "Resolved", target: "Closed", count: 9, cases: 9, median_hours: 1, p90_hours: 1 },
      { source: "Resolved", target: "Reopened", count: 2, cases: 2, median_hours: 1, p90_hours: 1 },
      { source: "Reopened", target: "Resolved", count: 2, cases: 2, median_hours: 1, p90_hours: 1 },
    ]);
    expect(Object.fromEntries(depth)).toEqual({ Created: 0, Assigned: 1, Resolved: 2, Reopened: 3, Closed: 3 });
  });

  it("formats durations like the analysis sentences", () => {
    expect([0.25, 6.24, 47.9, 72, null].map(fmtHours)).toEqual(["15 min", "6.2 h", "47.9 h", "3.0 days", "n/a"]);
  });

  it("is in the guide", () => {
    expect(CAPABILITIES.find((c) => c.id === "process")!.href(WS)).toBe(`/w/${WS}/work?tab=process`);
  });
});
