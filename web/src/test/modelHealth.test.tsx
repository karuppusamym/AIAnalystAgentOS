import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { session } from "../api";
import { REFUSAL_VIEWS } from "../lib/ask";
import { ModelHealthPanel } from "../pages/Admin";
import { MODEL_HEALTH, mockBackend, USER } from "./mockBackend";

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const url = String(input);
    if (url.includes("probe=true")) {
      const probed = { ...MODEL_HEALTH, providers: MODEL_HEALTH.providers.map((p) => ({ ...p, probe: { probed: false, ok: false, detail: p.message ?? "" } })) };
      return new Response(JSON.stringify(probed), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const r = mockBackend(method, url, null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("model health (admin)", () => {
  it("says plainly that the process has no API key, shows the cooldown and today's spend vs the cap", async () => {
    session.set("tok", USER);
    mockFetch();
    render(<ModelHealthPanel />);
    expect(await screen.findByText(/No API key in this process — set OPENROUTER_API_KEY for the api and worker containers/)).toBeTruthy();
    expect(screen.getByText("OPENROUTER_API_KEY missing")).toBeTruthy();
    expect(screen.getByText("42s left")).toBeTruthy();
    expect(screen.getByText(/above the 80% alert/)).toBeTruthy();
  });

  it("probes on request and shows the per-provider result", async () => {
    session.set("tok", USER);
    const fetchMock = mockFetch();
    render(<ModelHealthPanel />);
    fireEvent.click(await screen.findByText("Probe credits"));
    await waitFor(() => expect(screen.getByText("Probe")).toBeTruthy());
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("probe=true"))).toBe(true);
  });

  it("a spend-cap refusal in Ask points to writing the SQL", () => {
    expect(REFUSAL_VIEWS.spend_cap.action).toBe("explain");
  });
});

describe("spend counters and repeated key messages", () => {
  function withHealth(over: Record<string, unknown>) {
    const body = { ...MODEL_HEALTH, ...over };
    return vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
  }

  it("lite profile: no Redis but Postgres proves the caps, so billable calls are not reported as paused", async () => {
    session.set("tok", USER);
    withHealth({ counters_available: false, spend_counters: { store: "postgres", available: true } });
    render(<ModelHealthPanel />);
    await screen.findAllByText(/Spend today/);
    expect(screen.queryByText(/billable model calls are paused/)).toBeNull();
  });

  it("warns, naming the store, when the store that proves the caps is down", async () => {
    session.set("tok", USER);
    withHealth({ counters_available: true, spend_counters: { store: "postgres", available: false } });
    render(<ModelHealthPanel />);
    expect(await screen.findByText(/Spend counters \(postgres\) are unavailable/)).toBeTruthy();
  });

  it("two providers missing the same key show one message", async () => {
    session.set("tok", USER);
    const message = "No API key in this process — set OPENROUTER_API_KEY for the api and worker containers and restart them.";
    withHealth({ providers: MODEL_HEALTH.providers.map((p) => ({ ...p, message, key_present: false })) });
    render(<ModelHealthPanel />);
    expect(await screen.findAllByText(message)).toHaveLength(1);
  });
});
