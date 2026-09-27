import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { session } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { CAPABILITIES } from "../lib/guide";
import { to } from "../routes";
import { mockBackend, resetMockState, USER, WS } from "./mockBackend";

vi.setConfig({ testTimeout: 20000 });

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend((init?.method ?? "GET").toUpperCase(), String(input), typeof init?.body === "string" ? init.body : null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function urls(f: ReturnType<typeof mockFetch>, method: string, re: RegExp): URL[] {
  return (f.mock.calls as [string, RequestInit | undefined][])
    .filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u))).map(([u]) => new URL(String(u), "http://x"));
}

function renderTransfer() {
  return render(<AuthProvider><MemoryRouter initialEntries={[to.data(WS, "transfer")]}><AppRoutes /></MemoryRouter></AuthProvider>);
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

describe("Download context", () => {
  it("downloads the whole workspace as OKF, then one source as JSON", async () => {
    const f = mockFetch();
    const createObjectURL = vi.fn(() => "blob:mock-context");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    renderTransfer();
    const form = await screen.findByRole("form", { name: "Download the workspace context" });
    expect(screen.getByText(/Never included: passwords and secret references/)).toBeTruthy();
    fireEvent.click(within(form).getByRole("button", { name: "Download" }));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    const first = urls(f, "GET", /\/context\/export/)[0];
    expect(first.pathname).toBe(`/api/workspaces/${WS}/context/export`);
    expect(first.searchParams.get("format")).toBe("okf");
    expect(first.searchParams.has("source_id")).toBe(false);
    expect(await screen.findByText("Downloaded context.okf.zip.")).toBeTruthy();

    const scope = within(form).getByLabelText("Scope") as HTMLSelectElement;
    await waitFor(() => expect(within(scope).getByRole("option", { name: "Uploaded files (csv)" })).toBeTruthy());
    fireEvent.change(scope, { target: { value: "src_files" } });
    fireEvent.change(within(form).getByLabelText("Format"), { target: { value: "json" } });
    expect(within(form).getByText(/One versioned file with a content digest/)).toBeTruthy();
    fireEvent.click(within(form).getByRole("button", { name: "Download" }));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(2));
    const second = urls(f, "GET", /\/context\/export/)[1];
    expect([second.searchParams.get("format"), second.searchParams.get("source_id")]).toEqual(["json", "src_files"]);
  });
});

describe("What the agents see", () => {
  it("previews a purpose with token numbers, cache status and sections, and downloads it as text", async () => {
    const f = mockFetch();
    const createObjectURL = vi.fn(() => "blob:mock-preview");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const { container } = renderTransfer();
    const form = await screen.findByRole("form", { name: "Preview the model context" });
    const task = within(form).getByLabelText("Task") as HTMLSelectElement;
    await waitFor(() => expect(within(task).getByRole("option", { name: "Proposing hypotheses" })).toBeTruthy());
    fireEvent.change(task, { target: { value: "hypothesis_generation" } });
    fireEvent.change(within(form).getByLabelText("Question (optional)"), { target: { value: "Why are incidents breaching SLA?" } });
    fireEvent.click(within(form).getByRole("button", { name: "Preview" }));

    const view = await screen.findByLabelText("Context preview");
    const sent = urls(f, "GET", /\/context\/preview/)[0];
    expect([sent.searchParams.get("purpose"), sent.searchParams.get("question")]).toEqual(["hypothesis_generation", "Why are incidents breaching SLA?"]);
    expect(within(view).getByText("~1,852 tokens")).toBeTruthy();
    expect(within(view).getByText(/1,840 stable, cached between calls · 12 per call/)).toBeTruthy();
    expect(within(view).getByText(/Knowledge lookup already cached/)).toBeTruthy();
    const table = within(view).getByRole("table");
    expect(within(table).getByText("Tables and columns")).toBeTruthy();
    expect(within(table).getByText("nothing relevant")).toBeTruthy();
    expect(within(table).getAllByText("every call")).toHaveLength(1);
    expect(view.textContent).toContain("TABLE sn.incident");

    fireEvent.click(within(view).getByRole("button", { name: "Download as text" }));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(urls(f, "GET", /\/context\/preview/)[1].searchParams.get("download")).toBe("true");

    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it("shows the context cache to editors and lets the owner clear it", async () => {
    const f = mockFetch();
    renderTransfer();
    const card = (await screen.findByText("Context cache")).closest("section, .card, div") as HTMLElement;
    await waitFor(() => expect(screen.getByText("3 (1 compiled, 2 retrieval)")).toBeTruthy());
    expect(screen.getByText("4 times, 12,000 characters not rebuilt")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear cache" }));
    expect(await screen.findByText("Cleared 3 entries.")).toBeTruthy();
    expect(urls(f, "DELETE", /\/context\/cache$/)).toHaveLength(1);
    expect(card).toBeTruthy();
  });
});

describe("guide", () => {
  it("points the context download at the Import & export tab", () => {
    const entry = CAPABILITIES.find((c) => c.id === "context-export");
    expect(entry?.href(WS)).toBe(to.data(WS, "transfer"));
  });
});
