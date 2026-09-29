/**
 * N-9 what-if on a governed Ask answer: the panel appears only for a governed answer, sends the approved
 * query with the changes, and shows every scenario number labelled simulated beside the observed one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { session, type AskTurn, type Scenario } from "../api";
import { WhatIfPanel, buildScenario } from "../components/WhatIf";
import { USER, WS } from "./mockBackend";

const query = { metrics: ["revenue"], dimensions: ["region"], filters: [{ field: "priority", op: "<=" as const, value: 2 }] };
const turn = {
  id: "turn_1", workspace_id: WS, question: "revenue by region", status: "answered",
  provenance: { governance: "governed", semantic: { model_id: "m", model_version: 3, compiler_version: "semantic.v2", metrics: [], query } },
  result: { query_id: "q1", columns: ["region", "revenue"], rows: [["East", 400], ["West", 240]], row_count: 2, truncated: false },
} as unknown as AskTurn;

const cell = (o: number, s: number, adjusted: number[]) => ({
  metric: "revenue", observed: { value: o, basis: "observed" }, simulated: { value: s, basis: "simulated" },
  change: { value: s - o, basis: "simulated" }, change_pct: { value: (s - o) / o, basis: "simulated" }, adjusted_by: adjusted,
});
const scenario = {
  id: "scn_1", workspace_id: WS, name: "East up 10%", scenario_version: "whatif.v1", label: "simulated", publishable: false,
  spec: {}, spec_hash: "s".repeat(64), assumptions: ["Campaign in East", "revenue changes by +10% where region is East."],
  assumptions_hash: "a".repeat(64), baseline: { basis: "observed", query_id: "qry_base", sql_hash: "x", result_hash: "r".repeat(64), row_count: 2,
    truncated: false, semantic: {} }, remeasured: null, columns: ["region", "revenue"],
  rows: [{ key: { region: "East" }, cells: [cell(400, 440, [0])] }, { key: { region: "West" }, cells: [cell(240, 240, [])] }],
  totals: [cell(640, 680, [0])], summary: "Observed revenue (total over the rows shown): 640. Simulated revenue under this scenario: 680 (+40, +6.3%).",
  guard: { ok: true, problems: [] }, result_hash: "h".repeat(64), ask_turn_id: "turn_1", created_by: "u", created_at: null,
} as unknown as Scenario;

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("what-if scenarios", () => {
  it("builds the request from the approved query and only when every number is given", () => {
    const d = { kind: "scale" as const, metric: "revenue", value: "10", dimension: "region", member: "East" };
    expect(buildScenario("", query, [{ ...d, value: "" }], {}, "", null)).toBeNull();
    const body = buildScenario("", query, [d], { priority: "3" }, "Campaign in East\n\n", "turn_1")!;
    expect(body.semantic_query).toBe(query);
    expect(body.adjustments).toEqual([{ kind: "scale", metric: "revenue", percent: 10, segment: { dimension: "region", values: ["East"] } }]);
    expect(body.filter_overrides).toEqual([{ field: "priority", value: 3 }]);
    expect(body.assumptions).toEqual(["Campaign in East"]);
  });

  it("is absent on an ad hoc answer", () => {
    render(<WhatIfPanel turn={{ ...turn, provenance: { governance: "ad_hoc" } } as AskTurn} />);
    expect(screen.queryByRole("button", { name: "What-if scenario" })).toBeNull();
  });

  it("runs a scenario and labels every scenario number simulated", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) =>
      new Response(JSON.stringify(init?.method === "POST" ? scenario : []), { status: init?.method === "POST" ? 201 : 200,
        headers: { "Content-Type": "application/json" } }));
    render(<WhatIfPanel turn={turn} />);
    fireEvent.click(screen.getByRole("button", { name: "What-if scenario" }));
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText("Only where"), { target: { value: "region" } });
    fireEvent.change(screen.getByLabelText("Member"), { target: { value: "East" } });
    fireEvent.click(screen.getByRole("button", { name: "Run scenario" }));
    const view = await screen.findByRole("region", { name: "Scenario East up 10%" });
    expect(within(view).getByText(/Simulated, not observed/)).toBeTruthy();
    expect(within(view).getByText(/cannot be published/)).toBeTruthy();
    const table = within(view).getByRole("table");
    expect(within(table).getAllByText("simulated").length).toBe(2 + 3);  // two headers, one per scenario value (2 rows + total)
    expect(within(table).getByText("Total")).toBeTruthy();
    expect(within(view).getByRole("list", { name: "Assumptions" }).textContent).toContain("Campaign in East");
    const [url, init] = f.mock.calls.find(([, i]) => i?.method === "POST") as [string, RequestInit];
    expect(String(url)).toContain(`/workspaces/${WS}/scenarios`);
    const sent = JSON.parse(String(init.body));
    expect(sent.semantic_query).toEqual(query);
    expect(sent.ask_turn_id).toBe("turn_1");
    expect(sent.adjustments[0]).toEqual({ kind: "scale", metric: "revenue", percent: 10, segment: { dimension: "region", values: ["East"] } });
  });
});
