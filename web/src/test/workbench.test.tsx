/**
 * P4-07 remaining: column and glossary curation in Data, preview filters that either re-query or are
 * visibly non-interactive, stale-edit handling on versioned definitions, and event-stream reconnects
 * (attempt, retry delay, last update, reconnect now, resume from the cursor).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { session, subscribeRunEvents, type ArtifactDetail, type StreamInfo } from "../api";
import { curationOps } from "../components/ColumnCuration";
import { DashboardPreview } from "../components/DashboardPreview";
import { StreamIndicator } from "../pages/RunView";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { resetMockState, USER, WS } from "./mockBackend";

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  session.clear();
});

describe("column and glossary curation in Data", () => {
  it("saves a column's tags and its glossary alias as a person's fact in the brief", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/data/catalog`);
    fireEvent.click(await screen.findByRole("button", { name: /Columns \(1\)/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Curate priority" }));
    const form = screen.getByRole("form", { name: "Curate priority" });
    fireEvent.change(within(form).getByLabelText("Tags"), { target: { value: "restricted" } });
    fireEvent.change(within(form).getByLabelText("Glossary alias"), { target: { value: "urgency" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save curation" }));
    expect(await screen.findByText("Saved servicenow.incident.priority: tags, brief v4.")).toBeTruthy();
    expect(bodyOf(calls(f, "PUT", /\/assets\/ast_inc\/columns\/priority\/tags$/)[0][1])).toEqual({ tags: ["restricted"] });
    const [[, patch]] = calls(f, "PATCH", /\/brief$/);
    expect(new Headers(patch!.headers).get("If-Match")).toBe('"3"');
    expect(bodyOf(patch).ops).toEqual([{ op: "set", assertion: { group: "domain", field: "alias", subject: "servicenow.incident.priority", value: "urgency", note: null, evidence: [] } }]);
  });

  it("only states what changed", () => {
    expect(curationOps("s.t.c", "", "", "")).toEqual([]);
    expect(curationOps("s.t.c", "hours", "", "n").map((o) => o.assertion?.field)).toEqual(["unit"]);
  });
});

describe("preview filters never pretend", () => {
  it("dashboard filters in the preview are visibly not interactive", async () => {
    const art = { id: "art_d", workspace_id: WS, run_id: null, type: "dashboard", name: "d", version: 1, status: "draft", platform: null, external_id: null,
      external_url: null, creator_agent: null, creator_user: null, content_hash: "x", created_at: "", updated_at: "",
      content: { title: "D", native_filters: ["assignment_group"], layout: [{ kind: "filters", chart: null, row: 0, col: 0, width: 12, height: 1 }] } } as unknown as ArtifactDetail;
    mockFetch();
    render(<MemoryRouter><DashboardPreview wsId={WS} artifact={art} /></MemoryRouter>);
    expect(await screen.findByText(/not interactive in this preview/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /assignment_group/ })).toBeNull();
    expect(screen.getByText("assignment_group").textContent).not.toMatch(/▾/);
  });
});

describe("stale edits on versioned definitions", () => {
  it("a 412 on publish says someone changed it and reloads instead of overwriting", async () => {
    mockFetch((method, path) => (method === "POST" && /\/definitions\/[^/]+\/publish$/.test(path)
      ? { status: 412, body: { error: { code: "precondition_failed", message: "revision 1 is not current (2)", details: {} } } } : null));
    renderAt(`/w/${WS}/data/catalog?tab=definitions`);
    const draft = (await screen.findAllByRole("button", { name: "Publish" }))[0];
    fireEvent.click(draft);
    expect(await screen.findByText(/Someone changed this\./)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload the current version" })).toBeTruthy();
  });
});

function sse(text: string) {
  return new Response(new ReadableStream<Uint8Array>({ start(c) { c.enqueue(new TextEncoder().encode(text)); c.close(); } }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("event stream reconnects", () => {
  it("reports the attempt and retry delay, reconnects now on request and marks the resumed stream", async () => {
    session.set("tok", USER);
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(sse(`id: 4\ndata: ${JSON.stringify({ id: 4, type: "task.updated", payload: {}, actor: null, created_at: "" })}\n\n`))
      .mockRejectedValueOnce(new TypeError("network down"))
      .mockResolvedValueOnce(sse('event: end\ndata: {"status":"COMPLETED"}\n\n'));
    const statuses: [string, StreamInfo | undefined][] = [];
    let handle: ReturnType<typeof subscribeRunEvents> | null = null;
    const ended = new Promise<void>((resolve) => {
      handle = subscribeRunEvents(WS, "run_demo", {
        onEvent: () => undefined, onEnd: () => resolve(),
        onStatus: (state, _e, info) => {
          statuses.push([state, info]);
          if (state === "reconnecting" && info?.retryInMs) handle?.reconnectNow(); // a person presses "Reconnect now"
        },
      });
    });
    await ended;
    const reconnecting = statuses.find(([s, i]) => s === "reconnecting" && i?.retryInMs);
    expect(reconnecting?.[1]).toMatchObject({ lastEventId: 4 });
    expect(reconnecting?.[1]?.attempt).toBeGreaterThanOrEqual(1);
    const opens = statuses.filter(([s]) => s === "open");
    expect(opens[0][1]?.resumed).toBe(false);
    expect(opens.at(-1)?.[1]?.resumed).toBe(true);
    expect(String(fetchMock.mock.calls.at(-1)![0])).toMatch(/after_id=4$/);
  });

  it("the indicator says Reconnecting with the attempt, retry and last update, and offers Reconnect now", () => {
    const onReconnect = vi.fn();
    const last = new Date("2026-09-27T10:32:15");
    render(<StreamIndicator state="reconnecting" info={{ attempt: 2, retryInMs: 4000, lastEventId: 9 }} lastUpdate={last} onReconnect={onReconnect} />);
    const status = screen.getByRole("status");
    expect(status.textContent).toMatch(/Reconnecting \(attempt 2 · retry in 4 s · last update /);
    fireEvent.click(within(status).getByRole("button", { name: "Reconnect now" }));
    expect(onReconnect).toHaveBeenCalledTimes(1);
  });
});
