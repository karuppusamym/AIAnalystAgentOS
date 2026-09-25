import { describe, expect, it } from "vitest";
import { SSEParser } from "../lib/sse";

describe("SSEParser", () => {
  it("parses events split across arbitrary chunks", () => {
    const p = new SSEParser();
    const raw = 'id: 7\nevent: task.updated\ndata: {"id":7,"type":"task.updated"}\n\n';
    const out = [];
    for (const ch of raw.match(/.{1,5}/gs)!) out.push(...p.feed(ch));
    expect(out).toEqual([{ id: "7", event: "task.updated", data: '{"id":7,"type":"task.updated"}' }]);
  });

  it("handles CRLF, comments (keep-alives) and multi-line data", () => {
    const p = new SSEParser();
    const out = p.feed(": keep-alive\r\n\r\nevent: end\r\ndata: line1\r\ndata: line2\r\n\r\n");
    expect(out).toEqual([{ id: null, event: "end", data: "line1\nline2" }]);
  });

  it("does not split a CRLF that arrives across two chunks", () => {
    const p = new SSEParser();
    expect(p.feed("data: a\r")).toEqual([]);
    expect(p.feed("\n\r\n")).toEqual([{ id: null, event: "message", data: "a" }]);
  });

  it("keeps the last event id across messages", () => {
    const p = new SSEParser();
    const out = p.feed("id: 3\ndata: x\n\ndata: y\n\n");
    expect(out.map((m) => m.id)).toEqual(["3", "3"]);
  });
});
