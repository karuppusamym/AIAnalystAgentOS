/**
 * P7-19 agent forms in Work → Workflows: an owner describes an agent (the server compiles and checks the
 * manifest), publishes it, and a workflow step can use it. Capabilities the workspace has not granted
 * cannot be chosen, a refused grant shows the server's reason, and non-owners see no authoring actions.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { session, type User, type WorkspaceDetail } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { headersOf, mockBackend, RUN, USER, WORKSPACE, WS } from "./mockBackend";

type Handler = (method: string, path: string, body: string | null) => { status: number; body: unknown } | null;

function mockFetch(override?: Handler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? init.body : null;
    const o = override?.(method, new URL(String(input), "http://x").pathname, body);
    if (o) return new Response(JSON.stringify(o.body), { status: o.status, headers: { "Content-Type": "application/json" } });
    const r = mockBackend(method, String(input), body, headersOf(init));
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function LocationProbe() {
  const l = useLocation();
  return <div data-testid="location">{l.pathname + l.search}</div>;
}

const renderAt = (path: string) => render(
  <AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /><Routes><Route path="*" element={<LocationProbe />} /></Routes></MemoryRouter></AuthProvider>);

const calls = (f: ReturnType<typeof vi.spyOn>, method: string, re: RegExp) =>
  (f.mock.calls as [string, RequestInit | undefined][]).filter(([u, i]) => (i?.method ?? "GET") === method && re.test(String(u)));

beforeEach(() => session.set("mock-token", USER));
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

const OPTIONS = {
  capabilities: [
    { id: "skill.catalog_lookup", ref: "skill.catalog_lookup@1.0.0", summary: "Catalog lookup", side_effect: "none", tool: "metadata.read",
      certification: "certified", enabled: true, grantable: true, reason: null, default_input: { assets: "$scope.assets" }, default_reason: null },
    { id: "skill.row_count", ref: "skill.row_count@1.0.0", summary: "Row count", side_effect: "read_source", tool: "sql.execute",
      certification: "certified", enabled: false, grantable: false, reason: "skill.row_count@1.0.0 is not enabled in this workspace",
      default_input: null, default_reason: "needs asset, which only a model proposal can fill" },
  ],
  knowledge_sections: ["glossary", "business_rules"], output_types: ["agent_output"],
  limits: { llm_calls: 20, usd: 2, queries: 200, max_rows: 50000, max_steps: 20, pii_access: ["none", "restricted"] },
};

const row = (over: Record<string, unknown>) => ({ workspace_id: WS, version: 1, revision: 1, status: "draft", content_hash: "h",
  created_by: USER.id, published_by: null, retired_by: null, reason: null, published_at: null, retired_at: null, created_at: null,
  updated_at: null, ...over });

/** A stateful definitions store answering the agent form, definitions and run routes. */
function backend() {
  const defs: Record<string, unknown>[] = [];
  const handler: Handler = (method, path, body) => {
    if (method === "GET" && path === `/api/workspaces/${WS}/agent-form`) return { status: 200, body: OPTIONS };
    if (method === "POST" && path === `/api/workspaces/${WS}/agent-form`) {
      const form = JSON.parse(body || "{}") as { key: string; title: string; capabilities: string[] };
      if (form.capabilities.includes("skill.row_count")) return { status: 403, body: { error: { code: "policy_denied",
        message: "the agent would exceed this workspace's grants: skill.row_count@1.0.0 is not enabled in this workspace" } } };
      const d = row({ id: "defn_agent", kind: "agent", key: form.key, title: form.title, form, spec: { kind: "Agent", id: form.key } });
      defs.push(d);
      return { status: 201, body: d };
    }
    if (method === "GET" && path === `/api/workspaces/${WS}/definitions`) return { status: 200, body: { items: defs, next_cursor: null } };
    if (method === "POST" && path === `/api/workspaces/${WS}/definitions`) {
      const input = JSON.parse(body || "{}") as Record<string, unknown>;
      const d = row({ ...input, id: "defn_workflow" });
      defs.push(d);
      return { status: 201, body: d };
    }
    const pub = new RegExp(`^/api/workspaces/${WS}/definitions/([^/]+)/publish$`).exec(path);
    if (method === "POST" && pub) {
      const d = defs.find((x) => x.id === pub[1])!;
      Object.assign(d, { status: "published", revision: 2 });
      return { status: 200, body: d };
    }
    const one = new RegExp(`^/api/workspaces/${WS}/definitions/([^/]+)$`).exec(path);
    if (method === "GET" && one) return { status: 200, body: defs.find((x) => x.id === one[1]) };
    if (method === "GET" && path === "/api/capabilities") return { status: 200, body: { digest: "d", capabilities: [
      { id: "agent.metadata", kind: "Agent", summary: "Discover metadata", entry: "python:analystos.agents.metadata:run",
        enabled: true, available: true, side_effect: "read_source", certification: { status: "certified" } }] } };
    if (method === "POST" && path === `/api/workspaces/${WS}/analysis`) return { status: 200, body: { id: RUN } };
    return null;
  };
  return { defs, handler };
}

it("an owner creates an agent from the form, publishes it and a workflow step uses it", async () => {
  const b = backend();
  const f = mockFetch(b.handler);
  renderAt(`/w/${WS}/work?tab=workflows`);
  fireEvent.click(await screen.findByRole("button", { name: "Create agent" }));
  const form = await screen.findByRole("form", { name: "Agent form" });
  // A capability the workspace has not granted cannot be chosen, and says why.
  expect((within(form).getByRole("checkbox", { name: /skill\.row_count/ }) as HTMLInputElement).disabled).toBe(true);
  expect(within(form).getByText(/skill\.row_count@1\.0\.0 is not enabled in this workspace/)).toBeTruthy();
  fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Table notes" } });
  fireEvent.change(within(form).getByLabelText("Key"), { target: { value: "agent.table_notes" } });
  fireEvent.change(within(form).getByLabelText("Purpose"), { target: { value: "Note the columns of every selected table" } });
  fireEvent.click(within(form).getByRole("checkbox", { name: /skill\.catalog_lookup/ }));
  fireEvent.click(within(form).getByRole("checkbox", { name: "Run skill.catalog_lookup by default" }));
  fireEvent.click(within(form).getByRole("checkbox", { name: "glossary" }));
  fireEvent.change(within(form).getByLabelText("Output name"), { target: { value: "Table notes" } });
  expect(within(within(form).getByLabelText("Data access")).queryByRole("option", { name: "Personal data" })).toBeNull();
  fireEvent.click(within(form).getByRole("button", { name: "Save agent draft" }));
  await waitFor(() => expect(calls(f, "POST", /\/agent-form$/)).toHaveLength(1));
  const sent = JSON.parse(String(calls(f, "POST", /\/agent-form$/)[0][1]?.body));
  expect(sent).toMatchObject({ key: "agent.table_notes", capabilities: ["skill.catalog_lookup"], default_actions: ["skill.catalog_lookup"],
    knowledge: { sections: ["glossary"] }, output: { type: "agent_output", name: "Table notes" },
    autonomy: { mode: "deterministic", pii_access: "none" } });
  expect(sent).not.toHaveProperty("tools");
  fireEvent.click(await screen.findByRole("button", { name: "Publish agent.table_notes" }));
  await waitFor(() => expect(calls(f, "POST", /defn_agent\/publish$/)).toHaveLength(1));
  await screen.findByRole("button", { name: "New version of agent.table_notes" });

  fireEvent.click(screen.getByRole("button", { name: "Build workflow" }));
  const builder = await screen.findByRole("form", { name: "Workflow builder" });
  await within(builder).findByRole("option", { name: /agent\.table_notes — Table notes \(workspace agent\)/ });
  fireEvent.change(within(builder).getAllByLabelText("Title")[0], { target: { value: "Table notes" } });
  fireEvent.change(within(builder).getByLabelText("Key"), { target: { value: "table_notes" } });
  fireEvent.change(within(builder).getByLabelText("Purpose"), { target: { value: "Note every selected table" } });
  fireEvent.change(within(builder).getByLabelText("Step key"), { target: { value: "notes" } });
  fireEvent.change(within(builder).getAllByLabelText("Title")[1], { target: { value: "Write table notes" } });
  fireEvent.change(within(builder).getByLabelText("Agent"), { target: { value: "agent.table_notes" } });
  fireEvent.click(within(builder).getByRole("button", { name: "Save draft" }));
  await waitFor(() => expect(calls(f, "POST", /\/definitions$/)).toHaveLength(1));
  const created = JSON.parse(String(calls(f, "POST", /\/definitions$/)[0][1]?.body));
  expect(created.spec.spec.steps[0]).toMatchObject({ key: "notes", use: "agent.table_notes" });
  expect(created.spec.side_effect).toBe("write_internal");
});

it("a refused grant shows the server's reason", async () => {
  const b = backend();
  mockFetch((method, path, body) => {
    if (method === "GET" && path === `/api/workspaces/${WS}/agent-form`) {
      // A stale options list offering a capability the server no longer grants.
      return { status: 200, body: { ...OPTIONS, capabilities: OPTIONS.capabilities.map((c) => ({ ...c, grantable: true, reason: null })) } };
    }
    return b.handler(method, path, body);
  });
  renderAt(`/w/${WS}/work?tab=workflows`);
  fireEvent.click(await screen.findByRole("button", { name: "Create agent" }));
  const form = await screen.findByRole("form", { name: "Agent form" });
  fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Row counter" } });
  fireEvent.change(within(form).getByLabelText("Key"), { target: { value: "agent.row_counter" } });
  fireEvent.change(within(form).getByLabelText("Purpose"), { target: { value: "Count rows of the selected tables" } });
  fireEvent.click(within(form).getByRole("checkbox", { name: /skill\.row_count/ }));
  fireEvent.change(within(form).getByLabelText("Mode"), { target: { value: "propose" } });
  fireEvent.change(within(form).getByLabelText("Output name"), { target: { value: "Row counts" } });
  fireEvent.click(within(form).getByRole("button", { name: "Save agent draft" }));
  expect(await within(form).findByText(/exceed this workspace's grants/)).toBeTruthy();
  expect(b.defs).toHaveLength(0);
});

const EDITOR: User = { ...USER, id: "usr_editor", email: "editor@analystos.local", name: "Ed Editor", is_admin: false };

it("editors see workspace agents but cannot author them", async () => {
  session.set("mock-token", EDITOR);
  const b = backend();
  b.defs.push(row({ id: "defn_agent", kind: "agent", key: "agent.table_notes", title: "Table notes", status: "published" }));
  mockFetch((method, path, body) => {
    if (method === "GET" && path === "/api/auth/me") return { status: 200, body: EDITOR };
    if (method === "GET" && path === `/api/workspaces/${WS}`) return { status: 200, body: { ...WORKSPACE, role: "editor" } satisfies WorkspaceDetail };
    if (method === "GET" && path === "/api/workspaces") return { status: 200, body: [{ ...WORKSPACE, role: "editor" }] };
    return b.handler(method, path, body);
  });
  renderAt(`/w/${WS}/work?tab=workflows`);
  const list = await screen.findByRole("list", { name: "Workspace agents" });
  expect(within(list).getByText("Table notes")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Create agent" })).toBeNull();
  expect(screen.queryByRole("button", { name: /New version of/ })).toBeNull();
  expect(screen.getByRole("button", { name: "Build workflow" })).toBeTruthy();
});
