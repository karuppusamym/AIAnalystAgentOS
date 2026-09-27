import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Health } from "../api";
import { bannerProblems, StatusBanner } from "../components/StatusBanner";
import { HEALTH } from "./mockBackend";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const DOWN: Health = {
  ok: true, degraded: true, orchestrator: "temporal",
  problems: [
    { dependency: "worker", severity: "critical", message: "No worker is running: runs stay queued until `analystos worker` is started.",
      detail: "no worker is polling analystos-analysis" },
    { dependency: "superset", severity: "warning", message: "Superset is not reachable: new publications go to the in-platform preview.",
      detail: "connection refused" },
    { dependency: "sandbox", severity: "info", message: "The Python sandbox is unavailable here.", detail: null },
  ],
  checks: { worker: { ok: false, state: "down" }, superset: { ok: false, state: "down" }, sandbox: { ok: false, state: "down" } },
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("service status banner", () => {
  it("shows nothing when every dependency is up", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(HEALTH));
    render(<StatusBanner />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByLabelText("Service status")).toBeNull();
  });

  it("names a missing worker and an unreachable Superset, but not info-level problems", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json(DOWN));
    render(<StatusBanner />);
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toMatch(/No worker is running/);
    expect(banner.textContent).toMatch(/Superset is not reachable/);
    expect(banner.textContent).toMatch(/connection refused/);
    expect(banner.textContent).not.toMatch(/sandbox/i);
  });

  it("says the API is not responding when the health call fails, and recovers on a re-check", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(<StatusBanner />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/API is not responding/);
    fetchMock.mockImplementation(async () => json(HEALTH));
    fireEvent.click(screen.getByText("Check again"));
    await waitFor(() => expect(screen.queryByLabelText("Service status")).toBeNull());
  });

  it("gives up on a hung API instead of waiting forever", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(globalThis, "fetch").mockImplementation((_u, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }));
    render(<StatusBanner />);
    await vi.advanceTimersByTimeAsync(9000);
    expect((await screen.findByRole("alert")).textContent).toMatch(/no answer within 8 s/);
  });

  it("a Superset-only outage is a warning, not an alert", () => {
    const problems = bannerProblems({ health: { ...DOWN, problems: [DOWN.problems![1]] } });
    expect(problems.map((p) => p.severity)).toEqual(["warning"]);
    // an older API without `problems` still gets a banner from its checks
    expect(bannerProblems({ health: { ok: true, checks: { worker: { ok: false, error: "x" } } } })[0].dependency).toBe("worker");
    expect(bannerProblems({ health: [] as unknown as Health })).toEqual([]);
  });
});
