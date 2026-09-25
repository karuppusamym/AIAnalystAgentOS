import { afterEach, describe, expect, it, vi } from "vitest";
import { session, subscribeRunEvents, type RunEvent } from "../api";

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
});
