import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { session } from "../api";
import { DeliveryDestinations } from "../components/DeliveryDestinations";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const USER = { id: "u1", email: "a@b", name: "A", is_admin: false, active: true, attributes: {}, created_at: "" };
const DEST = {
  id: "dst_1", workspace_id: "ws_1", name: "ops hook", kind: "webhook", content_kinds: ["alert", "report"],
  config: { url: "https://hooks.example.com/x" }, has_secret: true, destination_hash: "h", status: "authorized",
  approval_id: "apr_1", authorized_until: "2026-12-27T00:00:00Z", authorized: true, summary: "webhook POST to https://hooks.example.com/x",
  approval: { id: "apr_1", status: "approved" }, revision: 1,
};
const DEAD = {
  id: "dlv_1", destination_id: "dst_1", subject_type: "alert", subject_id: "alr_1", status: "dead_letter", attempts: 5, max_attempts: 5,
  next_attempt_at: null, last_error: "hooks.example.com answered 503", created_at: "2026-09-28T10:00:00Z", delivered_at: null,
};

beforeEach(() => session.set("tok123", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("external delivery destinations (N-3)", () => {
  it("lists authorized destinations and redrives a dead letter", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const u = String(url);
      if (u.endsWith("/redrive") && init?.method === "POST") return jsonResponse({ ...DEAD, status: "queued", attempts: 0 });
      if (u.endsWith("/delivery-destinations")) return jsonResponse([DEST]);
      if (u.endsWith("/deliveries")) return jsonResponse([DEAD]);
      return jsonResponse([]);
    });
    render(<DeliveryDestinations wsId="ws_1" canEdit />);
    expect(await screen.findByText("webhook POST to https://hooks.example.com/x")).toBeTruthy();
    expect(screen.getByText("hooks.example.com answered 503")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => String(u).endsWith("/api/deliveries/dlv_1/redrive")
      && (i as RequestInit | undefined)?.method === "POST")).toBe(true));
  });

  it("registers a webhook with a secret reference and says it awaits approval", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (String(url).endsWith("/delivery-destinations") && init?.method === "POST") {
        return jsonResponse({ ...DEST, status: "pending", authorized: false, approval: { id: "apr_9", status: "pending" } });
      }
      return jsonResponse([]);
    });
    render(<DeliveryDestinations wsId="ws_1" canEdit />);
    fireEvent.click(await screen.findByRole("button", { name: "Add destination" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "ops hook" } });
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "webhook" } });
    fireEvent.change(screen.getByLabelText("Webhook URL"), { target: { value: "https://hooks.example.com/x" } });
    fireEvent.change(screen.getByLabelText("Signing secret reference"), { target: { value: "env:ANALYSTOS_WEBHOOK_OPS" } });
    fireEvent.click(screen.getByRole("button", { name: "Request authorization" }));
    expect(await screen.findByText(/sends nothing until an\s+approver decides/)).toBeTruthy();
    const post = fetchMock.mock.calls.find(([u, i]) => String(u).endsWith("/delivery-destinations") && (i as RequestInit)?.method === "POST");
    expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({
      name: "ops hook", kind: "webhook", config: { url: "https://hooks.example.com/x", secret_ref: "env:ANALYSTOS_WEBHOOK_OPS" },
      content_kinds: ["report", "alert"],
    });
  });

  it("hides editing for viewers", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      jsonResponse(String(url).endsWith("/delivery-destinations") ? [DEST] : [DEAD]));
    render(<DeliveryDestinations wsId="ws_1" canEdit={false} />);
    await screen.findByText("webhook POST to https://hooks.example.com/x");
    expect(screen.queryByRole("button", { name: "Add destination" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
