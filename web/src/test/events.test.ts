import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, session, streamAskTurn, subscribeRunEvents, type RunEvent } from "../api";

function sseResponse(text: string) {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      // two chunks, split mid-event
      const mid = Math.floor(text.length / 2);
      c.enqueue(enc.encode(text.slice(0, mid)));
      c.enqueue(enc.encode(text.slice(mid)));
      c.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const ev = (id: number, type = "task.updated") =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({ id, type, payload: { key: `k${id}` }, actor: null, created_at: "" })}\n\n`;

afterEach(() => {
  vi.restoreAllMocks();
  session.clear();
});

describe("subscribeRunEvents", () => {
  it("sends the bearer header, reconnects with after_id and stops on `end`", async () => {
    session.set("tokX", { id: "u", email: "e", name: "n", is_admin: false, active: true, attributes: {}, created_at: "" });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(sseResponse(ev(1) + ": keep-alive\n\n" + ev(2)))
      .mockResolvedValueOnce(sseResponse(ev(2) + ev(3) + 'event: end\ndata: {"status":"COMPLETED"}\n\n'));
    const got: RunEvent[] = [];
    const ended = new Promise<string | null>((resolve) => {
      subscribeRunEvents("ws_1", "run_1", { onEvent: (e) => got.push(e), onEnd: resolve });
    });
    await vi.advanceTimersByTimeAsync(2000);
    expect(await ended).toBe("COMPLETED");
    vi.useRealTimers();
    expect(got.map((e) => e.id)).toEqual([1, 2, 3]); // duplicate id 2 dropped after reconnect
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/workspaces/ws_1/analysis/run_1/events?after_id=0");
    expect(String(fetchMock.mock.calls[1][0])).toBe("/api/workspaces/ws_1/analysis/run_1/events?after_id=2");
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tokX");
  });

  it("stops for good on `revoked`: no reconnect, the reason is reported", async () => {
    session.set("tokX", { id: "u", email: "e", name: "n", is_admin: false, active: true, attributes: {}, created_at: "" });
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(sseResponse(ev(1) + 'event: revoked\ndata: {"reason":"access_revoked"}\n\n'));
    const got: RunEvent[] = [];
    const closed = new Promise<string | undefined>((resolve) => {
      subscribeRunEvents("ws_1", "run_1", {
        onEvent: (e) => got.push(e),
        onStatus: (state, error) => {
          if (state === "closed") resolve(error);
        },
      });
    });
    expect(await closed).toBe("Access to this run was revoked");
    expect(got.map((e) => e.id)).toEqual([1]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("signs out on `expired`", async () => {
    session.set("tokX", { id: "u", email: "e", name: "n", is_admin: false, active: true, attributes: {}, created_at: "" });
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(sseResponse('event: expired\ndata: {"reason":"token_expired"}\n\n'));
    const signedOut = new Promise<void>((resolve) => session.onUnauthorized(resolve));
    subscribeRunEvents("ws_1", "run_1", { onEvent: () => undefined });
    await signedOut;
    session.onUnauthorized(null);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("streamAskTurn", () => {
  it("rejects with 403 when access is revoked mid-turn, after the stages it did receive", async () => {
    session.set("tokX", { id: "u", email: "e", name: "n", is_admin: false, active: true, attributes: {}, created_at: "" });
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(sseResponse(
      'event: stage\ndata: {"key":"scope","text":"Checking"}\n\nevent: revoked\ndata: {"reason":"access_revoked"}\n\n'));
    const stages: unknown[] = [];
    const err = await streamAskTurn("ask_1", "q", undefined, { onStage: (s) => stages.push(s) }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(403);
    expect((err as ApiError).code).toBe("access_revoked");
    expect(stages).toHaveLength(1);
  });
});
