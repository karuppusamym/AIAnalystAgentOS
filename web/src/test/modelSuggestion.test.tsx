/**
 * Data → Definitions → Suggested model: facts and dimensions with key, grain and time column, joins
 * with their status, the gaps, candidate metrics; an editor measures keys and joins and proposes it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { session } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { headersOf, mockBackend, USER, WS } from "./mockBackend";

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend(init?.method ?? "GET", String(input), typeof init?.body === "string" ? init.body : null, headersOf(init));
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("suggested data model", () => {
  it("opens Definitions on the suggestion: star schema, keys, joins, gaps and candidate metrics", async () => {
    mockFetch();
    render(<AuthProvider><MemoryRouter initialEntries={[`/w/${WS}/data/catalog?tab=definitions`]}><AppRoutes /></MemoryRouter></AuthProvider>);
    const tables = await screen.findByRole("region", { name: "Tables" });
    expect(within(tables).getByText("Incidents")).toBeTruthy();
    expect(within(tables).getByText("declared by the source")).toBeTruthy();
    expect(within(tables).getByText("opened_at")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Star schemas" })).getByText("Group")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Joins" })).getByText("waiting for review")).toBeTruthy();
    expect(screen.getAllByText(/Group has no key/).length).toBeGreaterThan(0);
    expect(within(screen.getByRole("region", { name: "Candidate metrics" })).getByText("Incident count")).toBeTruthy();
  });

  it("measures keys and joins through the gateway, then proposes the model for approval", async () => {
    const f = mockFetch();
    render(<AuthProvider><MemoryRouter initialEntries={[`/w/${WS}/data/catalog?tab=definitions`]}><AppRoutes /></MemoryRouter></AuthProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Measure keys and joins" }));
    expect(await screen.findByText(/Measured 1 key and 1 join in 3 queries: all hold/)).toBeTruthy();
    expect(await screen.findByText("unique over 4,210 rows")).toBeTruthy();
    expect(screen.getByText("keeps row count")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Propose for approval" }));
    expect(await screen.findByText(/Proposed as model version 3; 1 join queued/)).toBeTruthy();
    await waitFor(() => expect((f.mock.calls as [string, RequestInit | undefined][])
      .some(([u, i]) => i?.method === "POST" && /suggestion\/propose$/.test(String(u)))).toBe(true));
  });
});
