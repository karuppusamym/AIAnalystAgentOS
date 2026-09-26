/**
 * Minimal Server-Sent Events parser + fetch-based stream reader.
 *
 * EventSource cannot send an Authorization header, so the UI reads the SSE response body with
 * fetch() + ReadableStream and parses the text/event-stream framing itself (WHATWG spec subset:
 * `id`, `event`, `data` (multi-line), comments, CRLF/LF line endings).
 */

export interface SSEMessage {
  id: string | null;
  event: string;
  data: string;
}

export class SSEParser {
  private buffer = "";
  private data: string[] = [];
  private event = "";
  private id: string | null = null;

  /** Feed a text chunk; returns every message completed by it. */
  feed(chunk: string): SSEMessage[] {
    this.buffer += chunk;
    const out: SSEMessage[] = [];
    // Normalise CRLF / CR to LF, but keep a trailing lone CR in the buffer (it may pair with an LF).
    let idx: number;
    while ((idx = this.nextLineBreak()) >= 0) {
      const breakLen = this.buffer[idx] === "\r" && this.buffer[idx + 1] === "\n" ? 2 : 1;
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + breakLen);
      const msg = this.line(line);
      if (msg) out.push(msg);
    }
    return out;
  }

  private nextLineBreak(): number {
    for (let i = 0; i < this.buffer.length; i++) {
      const c = this.buffer[i];
      if (c === "\n") return i;
      if (c === "\r") {
        // A CR at the very end might be the first half of CRLF: wait for more input.
        if (i === this.buffer.length - 1) return -1;
        return i;
      }
    }
    return -1;
  }

  private line(line: string): SSEMessage | null {
    if (line === "") {
      if (this.data.length === 0 && !this.event) {
        return null;
      }
      const msg: SSEMessage = { id: this.id, event: this.event || "message", data: this.data.join("\n") };
      this.data = [];
      this.event = "";
      return msg;
    }
    if (line.startsWith(":")) return null; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    switch (field) {
      case "data":
        this.data.push(value);
        break;
      case "event":
        this.event = value;
        break;
      case "id":
        if (!value.includes("\0")) this.id = value;
        break;
      default:
        break; // "retry" and unknown fields are ignored
    }
    return null;
  }
}

export interface StreamOptions {
  url: string;
  headers?: Record<string, string>;
  signal: AbortSignal;
  onMessage: (m: SSEMessage) => void;
  onOpen?: () => void;
  /** POST with a JSON body (a streamed Ask turn); GET when omitted. */
  method?: "GET" | "POST";
  body?: unknown;
}

/** Read one SSE response to completion (or abort). Throws on non-2xx. */
export async function readSSE({ url, headers, signal, onMessage, onOpen, method = "GET", body }: StreamOptions): Promise<void> {
  const init: RequestInit = { method, headers: { Accept: "text/event-stream", ...headers }, signal, cache: "no-store" };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers = { ...init.headers, "Content-Type": "application/json" };
  }
  const resp = await fetch(url, init);
  if (!resp.ok || !resp.body) {
    let message = `stream failed (${resp.status})`;
    try {
      const body = await resp.json();
      message = body?.error?.message ?? message;
    } catch {
      /* not JSON */
    }
    const err = new Error(message) as Error & { status?: number };
    err.status = resp.status;
    throw err;
  }
  onOpen?.();
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const m of parser.feed(decoder.decode(value, { stream: true }))) onMessage(m);
  }
  for (const m of parser.feed(decoder.decode() + "\n\n")) onMessage(m);
}
