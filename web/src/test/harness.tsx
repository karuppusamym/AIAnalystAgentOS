/**
 * Shared harness for the wave-2 suites: the app rendered at a URL over the in-memory backend (with
 * request headers, so revisioned edits carry If-Match), a location probe, and call inspection.
 */
import { vi } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { headersOf, mockBackend } from "./mockBackend";

export type Handler = (method: string, path: string, body: string | null, url: URL) => { status: number; body: unknown } | null;

export function mockFetch(override?: Handler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? init.body : null;
    const url = new URL(String(input), "http://x");
    const o = override?.(method, url.pathname, body, url);
    if (o) return new Response(JSON.stringify(o.body), { status: o.status, headers: { "Content-Type": "application/json" } });
    const r = mockBackend(method, String(input), body, headersOf(init));
    return new Response(r.status === 204 ? null : r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function LocationProbe() {
  const l = useLocation();
  return <div data-testid="location">{l.pathname + l.search}</div>;
}

export function renderAt(path: string) {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <Routes><Route path="*" element={<LocationProbe />} /></Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

/** The fetch calls with this method whose URL matches. */
export const calls = (f: ReturnType<typeof mockFetch>, method: string, re: RegExp) =>
  (f.mock.calls as [string, RequestInit | undefined][]).filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u)));

export const bodyOf = (init: RequestInit | undefined) => JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
