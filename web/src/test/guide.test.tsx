/**
 * The in-product Guide and tours: every link and tour step lands on a real route, tours only point at
 * anchors that exist in the code, the admin-only capabilities stay out of an analyst's map, and a tour
 * plays, steps back and ends without changing anything.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { matchPath, MemoryRouter } from "react-router-dom";
import { session, type User } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { placeCard, setTourTargetWait } from "../components/Tour";
import { CAPABILITIES, DISCIPLINES, TERMS, TOURS, toursDone } from "../lib/guide";
import { SCREENS } from "../routes";
import { headersOf, mockBackend, USER, WS } from "./mockBackend";

const SOURCES = import.meta.glob("../{pages,components}/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const ALL_SOURCE = Object.values(SOURCES).join("\n");

function routed(href: string): string | undefined {
  const path = href.split("?")[0];
  return SCREENS.find((s) => matchPath({ path: s.path, end: true }, path))?.id;
}

describe("guide content", () => {
  it("links every capability to a screen in the route manifest", () => {
    for (const c of CAPABILITIES) expect(routed(c.href(WS)), `${c.id} → ${c.href(WS)}`).toBeTruthy();
    expect(new Set(CAPABILITIES.map((c) => c.id)).size).toBe(CAPABILITIES.length);
    for (const d of DISCIPLINES) expect(CAPABILITIES.some((c) => c.discipline === d.id), d.id).toBe(true);
  });

  it("points every tour step at a real route and at an anchor that exists in the code", () => {
    for (const t of TOURS) {
      expect(t.steps.length, t.id).toBeGreaterThan(1);
      for (const step of t.steps) {
        if (step.path) expect(routed(step.path(WS)), `${t.id}: ${step.title}`).toBeTruthy();
        const m = step.target?.match(/^\[data-tour="([^"]+)"\]$/);
        if (!step.target) continue;
        expect(m, `${t.id}: ${step.target} must be a data-tour anchor`).toBeTruthy();
        const anchor = m![1];
        if (anchor.startsWith("nav-")) {
          expect(SCREENS.some((s) => s.nav && `nav-${s.id}` === anchor), anchor).toBe(true);
        } else if (anchor.startsWith("tab-")) {
          expect(ALL_SOURCE.includes(`id: "${anchor.slice(4)}"`), anchor).toBe(true);
        } else {
          expect(ALL_SOURCE.includes(`data-tour="${anchor}"`), anchor).toBe(true);
        }
      }
    }
  });

  it("explains its words without jargon codes", () => {
    for (const t of TERMS) {
      expect(t.meaning.length).toBeGreaterThan(20);
      expect(t.meaning).not.toMatch(/_[a-z]/);
    }
  });

  it("keeps a tour card inside the viewport", () => {
    const at = (left: number, top: number, w = 80, h = 30) => ({ left, top, right: left + w, bottom: top + h, width: w, height: h }) as DOMRect;
    const below = placeCard(at(1300, 100), 1440, 900);
    expect(below.top).toBe(144);
    expect(Number(below.left) + Number(below.width)).toBeLessThanOrEqual(1440 - 12);
    const above = placeCard(at(100, 820), 1440, 900);
    expect(Number(above.top)).toBeLessThan(820);
    const phone = placeCard(at(10, 400), 390, 844);
    expect(phone.width).toBe(360);
  });
});

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend(init?.method ?? "GET", String(input), typeof init?.body === "string" ? init.body : null, headersOf(init));
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function renderAt(path: string) {
  return render(<AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter></AuthProvider>);
}

const ANALYST: User = { ...USER, id: "usr_analyst", email: "analyst@analystos.local", name: "Ana Analyst", is_admin: false };

beforeEach(() => {
  session.set("mock-token", USER);
  setTourTargetWait(50);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
  try { window.localStorage.clear(); } catch { /* ignore */ }
});

describe("the Guide drawer", () => {
  it("maps what the platform can do, with tours and words, from the top bar", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    fireEvent.click(await screen.findByRole("button", { name: /Guide/ }));
    const drawer = await screen.findByRole("dialog", { name: "Guide" });
    for (const d of DISCIPLINES) expect(within(drawer).getByRole("heading", { name: d.label })).toBeTruthy();
    expect(within(drawer).getByText("AI model settings")).toBeTruthy();
    fireEvent.click(within(drawer).getByRole("tab", { name: "Tours" }));
    for (const t of TOURS) expect(within(drawer).getByText(t.title)).toBeTruthy();
    fireEvent.click(within(drawer).getByRole("tab", { name: "Words we use" }));
    expect(within(drawer).getByText("Governed / ad hoc")).toBeTruthy();
  });

  it("hides platform-admin capabilities from an analyst", async () => {
    session.set("mock-token", ANALYST);
    const f = mockFetch();
    f.mockImplementation(async (input, init) => {
      const path = new URL(String(input), "http://x").pathname;
      if (path === "/api/auth/me") return new Response(JSON.stringify(ANALYST), { status: 200, headers: { "Content-Type": "application/json" } });
      const r = mockBackend(init?.method ?? "GET", String(input), typeof init?.body === "string" ? init.body : null, headersOf(init));
      return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
    });
    renderAt(`/w/${WS}`);
    fireEvent.click(await screen.findByRole("button", { name: /Guide/ }));
    const drawer = await screen.findByRole("dialog", { name: "Guide" });
    expect(within(drawer).getByText("Ask a question")).toBeTruthy();
    for (const t of ["AI model settings", "Usage and token savings", "Capability registry"]) expect(within(drawer).queryByText(t)).toBeNull();
  });
});

describe("a tour", () => {
  it("plays over the real screens, goes back, and remembers a finished tour", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    fireEvent.click(await screen.findByRole("button", { name: /Guide/ }));
    const drawer = await screen.findByRole("dialog", { name: "Guide" });
    fireEvent.click(within(drawer).getByRole("tab", { name: "Tours" }));
    const row = within(drawer).getByText("Ask a question").closest("li")!;
    fireEvent.click(within(row).getByRole("button", { name: "Start" }));

    const card = await screen.findByRole("dialog", { name: "Type a question" });
    expect(card.textContent).toMatch(/step 1 of 3/);
    fireEvent.click(within(card).getByRole("button", { name: "Next" }));
    const second = await screen.findByRole("dialog", { name: "What you get back" });
    fireEvent.click(within(second).getByRole("button", { name: "Back" }));
    await screen.findByRole("dialog", { name: "Type a question" });
    fireEvent.click(within(screen.getByRole("dialog", { name: "Type a question" })).getByRole("button", { name: "Next" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "What you get back" })).getByRole("button", { name: "Next" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Threads" })).getByRole("button", { name: "Finish" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Threads" })).toBeNull());
    expect(toursDone()).toContain("ask");
  });

  it("ends on Escape without marking the tour taken", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    fireEvent.click(await screen.findByRole("button", { name: /Guide/ }));
    const drawer = await screen.findByRole("dialog", { name: "Guide" });
    fireEvent.click(within(drawer).getByRole("tab", { name: "Tours" }));
    fireEvent.click(within(within(drawer).getByText("The two-minute tour").closest("li")!).getByRole("button", { name: "Start" }));
    await screen.findByRole("dialog", { name: "Welcome to AnalystOS" });
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Welcome to AnalystOS" })).toBeNull());
    expect(toursDone()).not.toContain("platform");
  });
});
