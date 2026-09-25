import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { ApprovalCard } from "../components/ApprovalsPanel";
import { Markdown } from "../components/Markdown";
import { StatusBadge } from "../components/ui";
import { session, type Approval } from "../api";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("StatusBadge", () => {
  it("renders the status with its tone", () => {
    render(<StatusBadge status="WAITING_USER" />);
    const el = screen.getByText("WAITING_USER");
    expect(el.getAttribute("data-tone")).toBe("warning");
  });
});

describe("Markdown", () => {
  it("renders lists, bold and only safe links", () => {
    const { container } = render(<Markdown text={"# Title\n- **P1** grew [docs](https://x.test)\n- bad [link](javascript:alert(1))"} />);
    expect(container.querySelector("strong")?.textContent).toBe("P1");
    const links = container.querySelectorAll("a");
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("https://x.test");
    expect(container.innerHTML).not.toContain("javascript:");
  });
});

describe("Login", () => {
  it("shows the API error message on failed login", async () => {
    // A fresh Response per call: the login screen also asks which sign-in methods exist (/api/auth/providers).
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input).endsWith("/api/auth/providers")
        ? jsonResponse({ password: true, oidc: { enabled: false, name: "SSO", login_url: null } })
        : jsonResponse({ error: { code: "unauthenticated", message: "invalid email or password", details: {} } }, 401),
    );
    render(
      <AuthProvider>
        <MemoryRouter initialEntries={["/login"]}><AppRoutes /></MemoryRouter>
      </AuthProvider>,
    );
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@analystos.local" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("invalid email or password")).toBeTruthy();
    const [url, init] = fetchMock.mock.calls.find(([u]) => String(u) === "/api/auth/login")!;
    expect(url).toBe("/api/auth/login");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ email: "admin@analystos.local", password: "wrong" });
  });

  it("offers single sign-on when the API reports an identity provider", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ password: true, oidc: { enabled: true, name: "Corp SSO", login_url: "/api/auth/oidc/login" } }));
    render(
      <AuthProvider>
        <MemoryRouter initialEntries={["/login"]}><AppRoutes /></MemoryRouter>
      </AuthProvider>,
    );
    const link = await screen.findByRole("link", { name: "Sign in with Corp SSO" });
    expect(link.getAttribute("href")).toBe("/api/auth/oidc/login?return_to=%2F");
    expect(screen.getByLabelText("Password")).toBeTruthy();
  });

  it("redirects unauthenticated users to the login page", () => {
    render(
      <AuthProvider>
        <MemoryRouter initialEntries={["/w/ws_1/runs"]}><AppRoutes /></MemoryRouter>
      </AuthProvider>,
    );
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeTruthy();
  });
});

describe("ApprovalCard", () => {
  const approval: Approval = {
    id: "apr_1", workspace_id: "ws", run_id: "run_1", action: "publish_dashboard", risk_tier: "high", destination: "superset",
    affected_assets: ["incidents"], payload_hash: "abcdef0123456789abcdef", plan_hash: "123", policy_version: 2, requested_by: "usr_1",
    status: "pending", decided_by: null, decided_at: null, reason: null, expires_at: "2099-01-01T00:00:00Z",
    evidence: { governance_review: { ok: true, problems: [] }, jev_consequential: 0.9 }, created_at: "2026-01-01T00:00:00Z",
  };

  it("approves with a bearer token and reports the decision", async () => {
    session.set("tok123", { id: "u", email: "a@b", name: "A", is_admin: true, active: true, attributes: {}, created_at: "" });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ ...approval, status: "approved" }));
    const onDecided = vi.fn();
    render(<ApprovalCard approval={approval} onDecided={onDecided} />);
    expect(screen.getByText("risk: high")).toBeTruthy();
    expect(screen.getByText(/passed/)).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("Reason (optional)"), { target: { value: "looks right" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(onDecided).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/approvals/apr_1/approve");
    expect((init as RequestInit).method).toBe("POST");
    expect(((init as RequestInit).headers as Record<string, string>).Authorization).toBe("Bearer tok123");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ reason: "looks right" });
  });

  it("shows the server's error message when a decision is refused", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: { code: "forbidden", message: "separation of duties: requester cannot approve", details: {} } }, 403),
    );
    render(<ApprovalCard approval={approval} onDecided={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText("separation of duties: requester cannot approve")).toBeTruthy();
  });
});
