/**
 * Optional smoke test against a running API: renders every screen with real data.
 * Skipped unless ANALYSTOS_LIVE_API is set, e.g.
 *   ANALYSTOS_LIVE_API=http://localhost:8000 npm test
 */
import { describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { api, session } from "../api";

const LIVE = process.env.ANALYSTOS_LIVE_API;

async function visit(path: string, expectText: RegExp): Promise<string[]> {
  cleanup();
  render(<AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter></AuthProvider>);
  await waitFor(() => expect(document.body.textContent).toMatch(expectText), { timeout: 10000 });
  await new Promise((r) => setTimeout(r, 1000));
  return [...document.querySelectorAll(".alert-danger")].map((e) => `${path}: ${e.textContent}`);
}

describe.skipIf(!LIVE)("live API smoke", () => {
  it("renders every screen against the running API without errors", async () => {
    const realFetch = globalThis.fetch;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) =>
      realFetch(typeof input === "string" && input.startsWith("/") ? `${LIVE}${input}` : input, init));
    const r = await api.login(process.env.ANALYSTOS_LIVE_EMAIL ?? "admin@analystos.local", process.env.ANALYSTOS_LIVE_PASSWORD ?? "ChangeMe123!");
    session.set(r.access_token, r.user);
    const workspaces = await api.listWorkspaces();
    const ws = workspaces.find((w) => (w.counts?.dashboard ?? 0) > 0) ?? workspaces[0];
    const errors: string[] = [];
    errors.push(...await visit("/", /Workspaces/));
    errors.push(...await visit("/admin", /Agents/));
    if (ws) {
      errors.push(...await visit(`/w/${ws.id}`, /Key verified insights/));
      errors.push(...await visit(`/w/${ws.id}/sources`, /Relationships/));
      errors.push(...await visit(`/w/${ws.id}/runs`, /Analysis runs/));
      errors.push(...await visit(`/w/${ws.id}/ask`, /SQL console/));
      errors.push(...await visit(`/w/${ws.id}/governance`, /Workspace policy/));
      const runs = await api.listRuns(ws.id);
      if (runs[0]) {
        errors.push(...await visit(`/w/${ws.id}/runs/${runs[0].id}`, /Investigation/));
        errors.push(...await visit(`/w/${ws.id}/runs/${runs[0].id}/console`, /Agent console/));
      }
      const insights = await api.listInsights(ws.id);
      if (insights[0]) errors.push(...await visit(`/w/${ws.id}/insights/${insights[0].id}`, /REV verification/));
      const dash = await api.listArtifacts(ws.id, { type: "dashboard" });
      if (dash[0]) {
        errors.push(...await visit(`/w/${ws.id}/studio?artifact=${dash[0].id}`, /Versions/));
        expect(document.querySelectorAll(".dash-cell").length).toBeGreaterThan(0);
      }
    }
    // A failed run legitimately shows its own error banner.
    expect(errors.filter((e) => !e.includes("Run error"))).toEqual([]);
  }, 180000);
});
