/**
 * Investigation → Lineage tab: the run's provenance from GET /api/workspaces/{ws}/lineage?run_id=…,
 * drawn with the layered LineageGraph (objective → hypothesis → experiment → query → table …).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { session, type RunLineage } from "../api";
import { calls, mockFetch, renderAt, type Handler } from "./harness";
import { resetMockState, RUN, USER, WS } from "./mockBackend";

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

const LINEAGE: RunLineage = {
  nodes: [
    { type: "experiment", id: "exp_1" }, { type: "hypothesis", id: "hyp_1" }, { type: "insight", id: "ins_1" },
    { type: "objective", id: RUN }, { type: "query", id: "qry_1" }, { type: "report", id: "art_rep" }, { type: "run", id: RUN },
    { type: "table", id: "sn.incident" },
  ],
  edges: [
    { from: ["objective", RUN], relation: "asks", to: ["hypothesis", "hyp_1"] },
    { from: ["hypothesis", "hyp_1"], relation: "tested_by", to: ["experiment", "exp_1"] },
    { from: ["experiment", "exp_1"], relation: "derived_from", to: ["query", "qry_1"] },
    { from: ["query", "qry_1"], relation: "reads", to: ["table", "sn.incident"] },
    { from: ["insight", "ins_1"], relation: "supported_by", to: ["experiment", "exp_1"] },
    { from: ["run", RUN], relation: "reported_by", to: ["report", "art_rep"] },
    { from: ["report", "art_rep"], relation: "cites", to: ["insight", "ins_1"] },
  ],
  truncated: true,
};

function lineageApi(): Handler {
  return (method, path, _body, url) => {
    if (method === "GET" && path === `/api/workspaces/${WS}/lineage` && url.searchParams.get("run_id") === RUN) {
      return { status: 200, body: LINEAGE };
    }
    return null;
  };
}

describe("run lineage", () => {
  it("loads the run's edges only when the tab opens and draws them upstream to downstream", async () => {
    const f = mockFetch(lineageApi());
    renderAt(`/w/${WS}/work/investigations/${RUN}`);
    const tab = await screen.findByRole("tab", { name: "Lineage" });
    expect(calls(f, "GET", /\/lineage\?/)).toHaveLength(0);
    fireEvent.click(tab);
    const layers = await screen.findByRole("list", { name: /Lineage layers/ });
    await waitFor(() => expect(calls(f, "GET", /\/lineage\?run_id=run_demo/)).toHaveLength(1));
    expect(layers.textContent).toContain("sn.incident");
    expect(layers.textContent).toContain("art_rep");
    expect(screen.getByTitle(`run ${RUN}`).className).toContain("lineage-focus");
    expect(screen.getByText("7 edges")).toBeTruthy();
    expect(screen.getByText(/Showing the first 7 edges/)).toBeTruthy();
  });

  it("says so when the lineage cannot be loaded", async () => {
    mockFetch((method, path) => (method === "GET" && path.endsWith("/lineage")
      ? { status: 500, body: { error: { code: "internal", message: "lineage store unavailable" } } } : null));
    renderAt(`/w/${WS}/work/investigations/${RUN}`);
    fireEvent.click(await screen.findByRole("tab", { name: "Lineage" }));
    expect((await screen.findByRole("alert")).textContent).toContain("lineage store unavailable");
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
  });
});
