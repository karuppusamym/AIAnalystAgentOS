import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session } from "../api";
import { mockFetch, renderAt } from "./harness";
import { resetMockState, RUN_ROW, USER, WS } from "./mockBackend";

beforeEach(() => { resetMockState(); session.set("mock-token", USER); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

it("separates findings from two questions over the same workspace", async () => {
  const second = { ...RUN_ROW, id: "run_second", objective: "Why is customer retention falling?" };
  mockFetch((method, path) => {
    if (method === "GET" && path === `/api/workspaces/${WS}/analysis`) return { status: 200, body: [RUN_ROW, second] };
    if (method === "GET" && path === `/api/workspaces/${WS}/insights`) return { status: 200, body: [
      { id: "ins_first", workspace_id: WS, run_id: RUN_ROW.id, code: "F1", title: "P1 resolution rose", created_at: RUN_ROW.created_at,
        confidence: 0.8, status: "verified", verified: true },
      { id: "ins_second", workspace_id: WS, run_id: second.id, code: "F1", title: "Retention declined", created_at: RUN_ROW.created_at,
        confidence: 0.7, status: "verified", verified: true },
    ] };
    if (method === "GET" && path === `/api/workspaces/${WS}/artifacts`) return { status: 200, body: [] };
    return null;
  });
  renderAt(`/w/${WS}/outputs?type=finding`);
  const list = await screen.findByRole("list", { name: "Outputs" });
  expect(within(list).getByText("P1 resolution rose")).toBeTruthy();
  expect(within(list).getByText("Retention declined")).toBeTruthy();
  const investigations = await screen.findByRole("complementary", { name: "Investigations" });
  fireEvent.click(within(investigations).getByRole("button", { name: /Why is customer retention falling/ }));
  expect(within(list).queryByText("P1 resolution rose")).toBeNull();
  expect(within(list).getByText("Retention declined")).toBeTruthy();
});
