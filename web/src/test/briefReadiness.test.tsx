/**
 * P4-04 UI: Start work from the server's job kinds, the brief (facts vs open questions, origin and
 * review state), readiness with per-check reasons and remediation (never a score), and stale-edit
 * handling (412 → "someone changed this", reload and compare).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { session } from "../api";
import { calls, mockFetch, renderAt } from "./harness";
import { resetMockState, USER, WORKSPACE, WS } from "./mockBackend";
import { bumpBriefElsewhere } from "./mockWave2";

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
  try { window.localStorage.clear(); } catch { /* ignore */ }
});

describe("the brief in Data (P4-04)", () => {
  it("shows facts apart from open questions, each with its origin, review state and evidence", async () => {
    mockFetch();
    renderAt(`/w/${WS}/data/catalog?tab=brief`);
    expect(await screen.findByRole("tab", { name: "Brief & readiness", selected: true })).toBeTruthy();
    const questions = await screen.findByRole("region", { name: /Open questions \(2\)/ });
    const grain = within(questions).getByRole("listitem", { name: "Grain (one row per…) of stg_sn.incident" });
    expect(within(grain).getByText("inferred by a rule")).toBeTruthy();
    expect(within(grain).getByText("open question")).toBeTruthy();
    expect(within(grain).getByRole("list", { name: /Evidence for/ }).textContent).toMatch(/stg_sn\.incident\.sys_id/);
    const target = within(questions).getByRole("listitem", { name: /Target of stg_sn.incident/ });
    expect(within(target).getByText("suggested by a model")).toBeTruthy();
    const facts = screen.getByRole("region", { name: /Facts \(3\)/ });
    expect(within(facts).getByText("validated by a check")).toBeTruthy();
    expect(within(facts).getAllByText("stated by a person")).toHaveLength(2);
    // a fact is never offered for review again
    expect(within(facts).queryByRole("button", { name: /Confirm/ })).toBeNull();
  });

  it("confirms a suggestion against the version on screen (If-Match) and it becomes a fact", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/data/catalog?tab=brief`);
    const questions = await screen.findByRole("region", { name: /Open questions \(2\)/ });
    fireEvent.click(within(questions).getByRole("button", { name: "Confirm Grain (one row per…) of stg_sn.incident" }));
    await screen.findByText(/Confirmed\. Brief version 4\. 1 readiness assessment is now out of date\./);
    const [[, init]] = calls(f, "PATCH", /\/brief$/);
    expect(new Headers(init!.headers).get("If-Match")).toBe('"3"');
    expect(JSON.parse(String(init!.body))).toEqual({ ops: [{ op: "review", key: "data_semantics.grain:stg_sn.incident" }] });
    expect(await screen.findByRole("region", { name: /Facts \(4\)/ })).toBeTruthy();
  });

  it("states a glossary alias as a person's fact", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/data/catalog?tab=brief`);
    await screen.findByRole("region", { name: /Open questions/ });
    fireEvent.click(screen.getByText(/State a fact/));
    const form = screen.getByRole("form", { name: "State a fact" });
    expect((within(form).getByLabelText("Group") as HTMLSelectElement).value).toBe("domain");
    fireEvent.change(within(form).getByLabelText("About"), { target: { value: "MTTR" } });
    fireEvent.change(within(form).getByLabelText("Value"), { target: { value: "mean time to resolve" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save as a fact" }));
    await screen.findByText(/Saved\. Brief version 4/);
    const body = JSON.parse(String(calls(f, "PATCH", /\/brief$/)[0][1]!.body));
    expect(body.ops[0]).toEqual({ op: "set", assertion: { group: "domain", field: "alias", subject: "MTTR", value: "mean time to resolve", note: null, evidence: [] } });
    expect(await screen.findByRole("listitem", { name: "Alias of MTTR" })).toBeTruthy();
  });

  it("a stale edit (412) says someone changed this, compares, and reloads without overwriting", async () => {
    mockFetch();
    renderAt(`/w/${WS}/data/catalog?tab=brief`);
    const questions = await screen.findByRole("region", { name: /Open questions \(2\)/ });
    bumpBriefElsewhere(); // another editor saves version 4 meanwhile
    fireEvent.click(within(questions).getByRole("button", { name: /Reject Target of/ }));
    const alert = await screen.findByText(/Someone changed this\./);
    const box = alert.closest(".stale-edit") as HTMLElement;
    fireEvent.click(within(box).getByRole("button", { name: "Compare with mine" }));
    const diff = await within(box).findByRole("table", { name: /Differences between your version and the current one/ });
    expect(diff.textContent).toMatch(/priority one/);
    expect(diff.textContent).toMatch(/critical priority/);
    fireEvent.click(within(box).getByRole("button", { name: "Reload the current version" }));
    await waitFor(() => expect(screen.queryByText(/Someone changed this/)).toBeNull());
    expect(await screen.findByText(/Version 4\./)).toBeTruthy();
    // the rejected suggestion is still open: nothing was applied
    expect(screen.getByRole("region", { name: /Open questions \(2\)/ })).toBeTruthy();
  });
});

describe("readiness (P4-04): per-check reasons and remediation, never a score", () => {
  it("a blocked Predict names the failing required check, its fix, and an explicit alternative", async () => {
    mockFetch();
    renderAt(`/w/${WS}/data/catalog?tab=brief`);
    const form = await screen.findByRole("form", { name: "Check readiness" });
    fireEvent.change(within(form).getByLabelText("Kind of work"), { target: { value: "predict" } });
    fireEvent.change(within(form).getByLabelText("Target column"), { target: { value: "breached_sla" } });
    fireEvent.click(within(form).getByRole("button", { name: "Check readiness" }));
    const result = await screen.findByRole("group", { name: "Readiness for predict" });
    expect(within(result).getByText("Blocked")).toBeTruthy();
    const required = within(result).getByRole("table", { name: "Required checks" });
    const row = within(required).getByText("Labels exist and are known in time").closest("tr")!;
    expect(within(row).getByText("fail")).toBeTruthy();
    expect(row.textContent).toMatch(/known only after resolution/);
    expect(row.textContent).toMatch(/State when the label becomes known/);
    expect(within(result).getByRole("table", { name: "Advisory checks" }).textContent).toMatch(/warn/);
    expect(result.textContent).not.toMatch(/score|\d+%/i);
    fireEvent.click(within(result).getByRole("button", { name: "Choose explain" }));
    expect((within(form).getByLabelText("Kind of work") as HTMLSelectElement).value).toBe("explain");
  });
});

describe("onboarding shows the brief (read-only) at the review step", () => {
  it("the brief step summarises facts and open questions and links to Data", async () => {
    mockFetch((method, path) => {
      if (method !== "GET") return null;
      if (path === `/api/workspaces/${WS}`) return { status: 200, body: { ...WORKSPACE, objective: "" } };
      if (path === `/api/workspaces/${WS}/analysis`) return { status: 200, body: [] };
      if (path === `/api/workspaces/${WS}/catalog`) return { status: 200, body: [] };
      return null;
    });
    renderAt(`/w/${WS}`);
    const list = await screen.findByRole("list", { name: "Getting started" });
    const current = within(list).getAllByRole("listitem").find((s) => s.getAttribute("aria-current") === "step")!;
    expect(within(current).getByText("Review what the data means")).toBeTruthy();
    expect(await within(current).findByText(/3 facts confirmed, 2 open questions\./)).toBeTruthy();
    expect(within(current).getByRole("list", { name: "Open questions in the brief" }).textContent).toMatch(/one row per incident/);
    expect(within(current).getByRole("link", { name: "Review the brief" }).getAttribute("href")).toBe(`/w/${WS}/data/catalog?tab=brief`);
    expect(within(current).queryByRole("button", { name: /Confirm/ })).toBeNull();
  });
});
