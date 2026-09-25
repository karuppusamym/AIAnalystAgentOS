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
import { to } from "../routes";

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
    errors.push(...await visit(to.registry(), /Agents/));
    errors.push(...await visit(to.settings(), /Platform settings/));
    errors.push(...await visit(to.usage(), /Token savings/));
    if (ws) {
      errors.push(...await visit(to.workspace(ws.id), /What changed/));
      errors.push(...await visit(to.sources(ws.id), /Relationships/));
      errors.push(...await visit(to.catalog(ws.id), /Catalog/));
      errors.push(...await visit(to.investigations(ws.id), /Investigations/));
      errors.push(...await visit(to.ask(ws.id), /SQL console/));
      errors.push(...await visit(to.governance(ws.id), /Workspace policy/));
      errors.push(...await visit(to.approvals(ws.id), /Approvals/));
      const runs = await api.listRuns(ws.id);
      if (runs[0]) {
        errors.push(...await visit(to.run(ws.id, runs[0].id), /Investigation/));
        errors.push(...await visit(to.runConsole(ws.id, runs[0].id), /Agent console/));
      }
      errors.push(...await visit(to.schedules(ws.id), /Schedules/));
      errors.push(...await visit(to.monitoring(ws.id), /Monitors/));
      errors.push(...await visit(to.monitoring(ws.id, { tab: "alerts" }), /Alerts/));
      errors.push(...await visit(to.reports(ws.id), /Generate report/));
      const scheduled = runs.find((r) => r.origin?.type === "schedule" && r.status === "COMPLETED");
      if (scheduled) errors.push(...await visit(to.run(ws.id, scheduled.id), /What changed since the previous run/));
      const insights = await api.listInsights(ws.id);
      if (insights[0]) errors.push(...await visit(to.findings(ws.id, insights[0].id), /REV verification/));
      const dash = await api.listArtifacts(ws.id, { type: "dashboard" });
      if (dash[0]) {
        errors.push(...await visit(to.studio(ws.id, dash[0].id), /Versions/));
        expect(document.querySelectorAll(".dash-cell").length).toBeGreaterThan(0);
      }
    }
    // A failed run legitimately shows its own error banner.
    expect(errors.filter((e) => !e.includes("Run error"))).toEqual([]);
  }, 180000);
});
