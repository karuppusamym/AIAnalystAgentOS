import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, matchPath, Route, Routes, useLocation } from "react-router-dom";
import axe from "axe-core";
import { session } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { filterItems, screenItems } from "../components/CommandPalette";
import { ThemeToggle } from "../components/Layout";
import { ConfidenceBar, Stat, StateView, Value } from "../components/ui";
import { fmtMs, fmtUsd, formatKnown } from "../lib/format";
import { getThemePref, nextThemePref, resolveTheme, setThemePref } from "../lib/theme";
import { fillPath, JOURNEYS, LEGACY_REDIRECTS, SCREEN_BUDGET, SCREENS, to } from "../routes";
import { INSIGHT, mockBackend, RUN, USER, WS } from "./mockBackend";

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend(init?.method ?? "GET", String(input), typeof init?.body === "string" ? init.body : null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function LocationProbe() {
  const l = useLocation();
  return <div data-testid="location">{l.pathname + l.search}</div>;
}

function renderAt(path: string) {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <Routes><Route path="*" element={<LocationProbe />} /></Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
  setThemePref("system");
});

// ------------------------------------------------------------------------------------ information architecture
describe("route manifest (spec v3 §9)", () => {
  it(`has at most ${SCREEN_BUDGET} screens, each with a unique id and path`, () => {
    expect(SCREEN_BUDGET).toBe(20);
    expect(SCREENS.length).toBeLessThanOrEqual(SCREEN_BUDGET);
    expect(new Set(SCREENS.map((s) => s.id)).size).toBe(SCREENS.length);
    expect(new Set(SCREENS.map((s) => s.path)).size).toBe(SCREENS.length);
  });

  it("gives every journey at least one navigable screen, in the spec's order", () => {
    expect(JOURNEYS.map((j) => j.label)).toEqual(["Home", "Ask", "Investigate", "Knowledge", "Build", "Operate"]);
    for (const j of JOURNEYS) expect(SCREENS.some((s) => s.journey === j.id && s.nav)).toBe(true);
  });

  it("places the increment-3 catalog/crawl panels in Knowledge and settings/token savings/usage in Operate", () => {
    const journeyOf = (id: string) => SCREENS.find((s) => s.id === id)?.journey;
    expect(journeyOf("catalog")).toBe("knowledge");
    expect(journeyOf("sources")).toBe("knowledge");
    expect(journeyOf("settings")).toBe("operate");
    expect(journeyOf("usage")).toBe("operate");
    expect(journeyOf("schedules")).toBe("operate");
    expect(journeyOf("monitoring")).toBe("operate");
    expect(journeyOf("reports")).toBe("build");
  });

  it("maps every legacy route onto a screen in the manifest", () => {
    for (const r of LEGACY_REDIRECTS) {
      const target = fillPath(r.to, { wsId: "ws", runId: "r", insightId: "i" });
      expect(SCREENS.some((s) => matchPath(s.path, target)), `${r.from} -> ${r.to}`).toBe(true);
    }
  });

  it.each([
    ["/admin", "/operate/registry"],
    ["/w/ws_1/sources", "/w/ws_1/knowledge/sources"],
    ["/w/ws_1/catalog", "/w/ws_1/knowledge/catalog"],
    ["/w/ws_1/runs", "/w/ws_1/investigate"],
    ["/w/ws_1/runs/run_9", "/w/ws_1/investigate/run_9"],
    ["/w/ws_1/runs/run_9/console", "/w/ws_1/investigate/run_9/console"],
    ["/w/ws_1/insights/ins_2", "/w/ws_1/investigate/findings/ins_2"],
    ["/w/ws_1/studio?artifact=art_1", "/w/ws_1/build/studio?artifact=art_1"],
    ["/w/ws_1/reports?artifact=art_2", "/w/ws_1/build/reports?artifact=art_2"],
    ["/w/ws_1/schedules?schedule=sch_1", "/w/ws_1/operate/schedules?schedule=sch_1"],
    ["/w/ws_1/monitoring?tab=alerts&alert=alr_1", "/w/ws_1/operate/monitoring?tab=alerts&alert=alr_1"],
    ["/w/ws_1/governance", "/w/ws_1/operate/governance"],
  ])("redirects %s to %s, keeping the query", async (from, expected) => {
    mockFetch();
    renderAt(from);
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(expected));
  });

  it("builds links with encoded parameters and optional segments", () => {
    expect(to.findings("ws 1")).toBe("/w/ws%201/investigate/findings");
    expect(to.findings("ws", "ins_1")).toBe("/w/ws/investigate/findings/ins_1");
    expect(to.monitoring("ws", { tab: "alerts" })).toBe("/w/ws/operate/monitoring?tab=alerts");
    expect(to.settings()).toBe("/operate/settings");
  });

  it("groups the side nav by journey", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const label of ["Home", "Ask", "Investigate", "Knowledge", "Build", "Operate"]) {
      expect(within(nav).getByRole("group", { name: label })).toBeTruthy();
    }
    expect(within(within(nav).getByRole("group", { name: "Knowledge" })).getByRole("link", { name: "Knowledge studio" }).getAttribute("href"))
      .toBe(`/w/${WS}/knowledge/catalog`);
    await screen.findByRole("heading", { name: "IT Service Management", level: 1 });
  });
});

// ------------------------------------------------------------------------------------ state families
describe("unknown is never shown as 0", () => {
  it("renders absent values as an explicit unknown state and real zeros as 0", () => {
    const { container } = render(<div>
      <span data-testid="none"><Value value={null} format="usd" /></span>
      <span data-testid="undef"><Value value={undefined} format="int" /></span>
      <span data-testid="nan"><Value value={Number.NaN} /></span>
      <span data-testid="zero"><Value value={0} format="int" /></span>
      <span data-testid="usd"><Value value={0.0421} format="usd" /></span>
      <span data-testid="suffix"><Value value={1200} format="int" suffix="tokens" /></span>
    </div>);
    for (const id of ["none", "undef", "nan"]) {
      const el = screen.getByTestId(id);
      expect(el.textContent).toMatch(/unknown/);
      expect(el.textContent).not.toMatch(/\b0\b|\$0/);
      expect(el.querySelector("[data-state=unknown]")).toBeTruthy();
    }
    expect(screen.getByTestId("zero").textContent).toBe("0");
    expect(screen.getByTestId("usd").textContent).toBe("$0.0421");
    expect(screen.getByTestId("suffix").textContent).toBe("1,200 tokens");
    expect(container.querySelectorAll("[data-state=known]").length).toBe(3);
  });

  it("formats with the same rule outside components", () => {
    expect(fmtUsd(null)).toBe("unknown");
    expect(fmtUsd(undefined)).toBe("unknown");
    expect(fmtUsd(0)).toBe("$0.0000");
    expect(fmtMs(undefined)).toBe("unknown");
    expect(formatKnown("", "int")).toBeNull();
    expect(formatKnown(true, "int")).toBeNull();
    expect(formatKnown(0.25, "pct")).toBe("25.0%");
  });

  it("shows unknown KPI tiles and confidence as unknown", () => {
    render(<div><Stat label="Cost" value={<Value value={undefined} format="usd" />} /><ConfidenceBar value={null} /></div>);
    expect(screen.getAllByText(/unknown/)).toHaveLength(2);
    expect(screen.queryByText("0%")).toBeNull();
  });

  it("marks home KPIs unknown when their request fails, instead of 0", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (String(input).includes("/alerts")) return new Response("{}", { status: 500 });
      const r = mockBackend(init?.method ?? "GET", String(input));
      return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
    });
    renderAt(`/w/${WS}`);
    const card = (await screen.findByRole("heading", { name: "What changed" })).closest("section")!;
    await waitFor(() => expect(within(card).getByText("Pending approvals").previousElementSibling?.textContent).toBe("1"));
    expect(within(card).getByText("Open alerts").previousElementSibling?.textContent).toMatch(/unknown/);
  });

  it("gives each state family its own labelled view", () => {
    render(<StateView kind="not-entitled" title="Admins only">Ask an administrator.</StateView>);
    expect(screen.getByRole("status").getAttribute("data-state")).toBe("not-entitled");
    expect(screen.getByText("not entitled")).toBeTruthy();
  });
});

// ------------------------------------------------------------------------------------ command palette
describe("Ctrl/Cmd-K command palette", () => {
  it("lists every navigable screen for the current workspace and filters by terms", () => {
    const items = screenItems(WS, "ITSM");
    expect(items.map((i) => i.label)).toContain("Knowledge studio");
    expect(items.find((i) => i.label === "Platform settings")?.href).toBe("/operate/settings");
    expect(screenItems(undefined).some((i) => i.href.includes(":wsId"))).toBe(false);
    expect(filterItems(items, "token").map((i) => i.label)).toEqual(["Usage & cost"]);
    expect(filterItems(items, "know cat").map((i) => i.label)).toEqual(["Knowledge studio"]);
  });

  it("opens with Ctrl-K as a modal dialog, navigates with the keyboard and closes with Esc", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    await screen.findByRole("heading", { name: "IT Service Management", level: 1 });
    const trigger = screen.getByRole("button", { name: /Go to/ });
    trigger.focus();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = await screen.findByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    const input = within(dialog).getByRole("combobox");
    expect(document.activeElement).toBe(input);
    // recent run and workspace entries load in
    await within(dialog).findByRole("option", { name: /Why are P1 resolution times rising/ });
    fireEvent.change(input, { target: { value: "catalog" } });
    expect(within(dialog).getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/knowledge/catalog`));
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(window, { key: "K", metaKey: true });
    const again = await screen.findByRole("dialog");
    fireEvent.keyDown(within(again).getByRole("combobox"), { key: "ArrowDown" });
    expect(within(again).getAllByRole("option")[1].getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(within(again).getByRole("combobox"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("traps Tab focus inside the dialog", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByRole("combobox");
    const close = within(dialog).getByRole("button", { name: /close/ });
    close.focus();
    fireEvent.keyDown(close, { key: "Tab" });
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(close);
  });
});

// ------------------------------------------------------------------------------------ theme
describe("theme tokens", () => {
  it("cycles system → light → dark, sets data-theme and persists the choice", () => {
    render(<ThemeToggle />);
    expect(getThemePref()).toBe("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    fireEvent.click(screen.getByRole("button"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(window.localStorage.getItem("analystos.theme")).toBe("light");
    fireEvent.click(screen.getByRole("button"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    fireEvent.click(screen.getByRole("button"));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem("analystos.theme")).toBeNull();
    expect(nextThemePref("dark")).toBe("system");
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
  });

  it("still switches when storage throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    act(() => setThemePref("dark"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});

// ------------------------------------------------------------------------------------ accessibility (axe)
/** The main screen of each journey, rendered in the full shell with the mock backend. */
const JOURNEY_SCREENS: [string, string, RegExp][] = [
  ["Home", `/w/${WS}`, /What changed/],
  ["Ask", `/w/${WS}/ask`, /SQL console/],
  ["Investigate", `/w/${WS}/investigate/${RUN}`, /Why are P1 resolution times rising/],
  ["Investigate · findings", `/w/${WS}/investigate/findings/${INSIGHT}`, /Network group drives P1 breaches/],
  ["Knowledge", `/w/${WS}/knowledge/catalog`, /Incidents/],
  ["Build", `/w/${WS}/build/studio`, /P1 resolution/],
  ["Operate", `/w/${WS}/operate/approvals`, /Publish dashboards/],
  ["Operate · settings", "/operate/settings", /Platform settings/],
];

describe("accessibility (axe-core, jsdom)", () => {
  it.each(JOURNEY_SCREENS)("%s screen has no axe violations", async (_name, path, ready) => {
    mockFetch();
    const { container } = renderAt(path);
    await waitFor(() => expect(container.textContent).toMatch(ready), { timeout: 4000 });
    await new Promise((r) => setTimeout(r, 50));
    // Colour contrast needs real layout; the Playwright axe run covers it in a browser.
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`)).toEqual([]);
  }, 15000);
});
