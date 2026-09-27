/**
 * Pipelines UI (P6-03, workbench-ux §5 Engineer journey) in Work → Prepare data: the source-to-output
 * DAG and transformation diff; dry-run join diagnostics, contract gates and reconciliation, with
 * quarantined and late rows shown on their own; approve → materialize; rollback in Outputs; failures
 * and freshness in Operate.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { session } from "../api";
import { DryRunResult } from "../components/Pipelines";
import { dryRun } from "./mockPipelines";
import { calls, bodyOf, mockFetch, renderAt } from "./harness";
import { decide } from "./mockApprovals";
import { resetMockState, USER, WS } from "./mockBackend";

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

async function openPipeline(version = 2) {
  const card = await screen.findByRole("region", { name: "Pipelines" });
  fireEvent.click(await within(card).findByRole("button", { name: new RegExp(`p1_clean v${version}`) }));
  return screen.findByRole("region", { name: `Pipeline p1_clean v${version}` });
}

describe("pipelines in Work → Prepare data (P6-03)", () => {
  it("shows the source-to-output DAG and the transformation diff against the previous version", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work?tab=prepare`);
    const detail = await openPipeline();
    const dag = within(detail).getByRole("list", { name: "Source to output" });
    const nodes = within(dag).getAllByRole("listitem").map((li) => li.querySelector("code")!.textContent);
    expect(nodes).toEqual(["stg_sn.incident", "stg_sn.sys_user_group", "p1_incidents_clean", "join_groups", "clean", "aos_out.p1_clean"]);
    fireEvent.click(within(detail).getByText("Transformation diff"));
    expect(within(detail).getByText(/Version 2 against version 1: \d+ changes/)).toBeTruthy();
    const diff = within(detail).getByRole("table", { name: "Changes from version 1" });
    expect(diff.textContent).toMatch(/joins/);
    expect(diff.textContent).toMatch(/added/);
  });

  it("the last run was blocked: the reason is shown and the last good version stays", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work?tab=prepare&pipeline=pip_2`);
    const run = await screen.findByRole("region", { name: "Dry run prn_0" });
    expect(within(run).getByText(/Blocked: a check failed, nothing was written and the last good version stays/)).toBeTruthy();
    expect(within(run).getByText(/3 rows failed a blocking gate/)).toBeTruthy();
  });

  it("dry run → ledger with quarantined and late rows apart → join diagnostics, gates, reconciliation → approve → materialize", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work?tab=prepare&pipeline=pip_2`);
    const detail = await screen.findByRole("region", { name: "Pipeline p1_clean v2" });
    fireEvent.click(within(detail).getByRole("button", { name: "Dry run" }));
    const run = await within(detail).findByRole("region", { name: "Dry run prn_1" });
    const ledger = within(run).getByRole("group", { name: "Rows" });
    const stat = (label: string) => within(ledger).getByText(label).closest(".stat") as HTMLElement;
    expect(stat("Input rows").textContent).toMatch(/4,222/);
    expect(stat("Output rows").textContent).toMatch(/4,190/);
    expect(stat("Quarantined rows").textContent).toMatch(/20/);
    expect(stat("Quarantined rows").getAttribute("data-attention")).toBe("true");
    expect(stat("Late rows").textContent).toMatch(/not reported/); // unknown, never a silent 0
    const checks = within(run).getByRole("table", { name: "Checks" });
    expect(within(checks).getByText("rows quarantined")).toBeTruthy();
    expect(checks.textContent).toMatch(/join_groups: declared many_to_one, observed many_to_one/);
    expect(within(run).getByRole("table", { name: "Join diagnostics" }).textContent).toMatch(/3 \(0\.0713%\)/);
    expect(within(run).getByRole("table", { name: "Reconciliation" }).textContent).toMatch(/hours_total/);

    fireEvent.click(within(detail).getByRole("button", { name: "Materialize this candidate" }));
    const pending = await within(detail).findByRole("status", { name: /Approval for writing aos_out\.p1_clean from dry run prn_1/ });
    fireEvent.click(within(pending).getByRole("button", { name: "Continue with the approved request" }));
    expect(await within(detail).findByText(/not approved yet/)).toBeTruthy();
    decide("apr_mat", "approve");
    fireEvent.click(within(pending).getByRole("button", { name: "Continue with the approved request" }));
    const done = await within(detail).findByText(/Promoted/);
    expect(done.closest(".alert")!.textContent).toMatch(/aos_out\.p1_clean version 3 \(4,190 rows\)/);
    expect(bodyOf(calls(f, "POST", /\/pipeline-runs\/prn_1\/materialize$/).at(-1)![1])).toEqual({ approval_id: "apr_mat" });
  });
});

describe("Outputs → Managed tables: rollback", () => {
  it("rolls the current version back to the one it replaced", async () => {
    mockFetch();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt(`/w/${WS}/outputs?type=table`);
    const table = await screen.findByRole("region", { name: "Table aos_out.p1_clean" });
    expect(within(table).getByText("superseded")).toBeTruthy();
    fireEvent.click(within(table).getByRole("button", { name: "Roll back v2" }));
    expect(await screen.findByText(/Rolled back: aos_out\.p1_clean serves version 1 again; version 2 is kept as rolled back\./)).toBeTruthy();
    expect(await within(table).findByText("rolled back")).toBeTruthy();
  });
});

describe("Operate → Models & pipelines: failures and freshness", () => {
  it("lists a blocked run with its reason and a safe next step, and a stale destination", async () => {
    mockFetch();
    renderAt(`/w/${WS}/operate/monitoring?tab=health`);
    const failures = await screen.findByRole("list", { name: "Failed and blocked pipeline runs" });
    expect(failures.textContent).toMatch(/3 rows failed a blocking gate/);
    expect(failures.textContent).toMatch(/the last good version keeps serving/);
    expect(failures.textContent).toMatch(/no automatic retry/);
    const stale = await screen.findByRole("list", { name: "Stale destinations" });
    expect(stale.textContent).toMatch(/aos_out\.p1_clean/);
    expect(stale.textContent).toMatch(/the pipeline promises 24 hours/);
  });
});

describe("late rows in the dry-run ledger", () => {
  const lateStat = () => within(screen.getByRole("group", { name: "Rows" })).getByText("Late rows").closest(".stat") as HTMLElement;

  it("shows a measured count, and an unmeasured one as not measured with the server's reason", () => {
    const base = dryRun();
    const { unmount } = render(<DryRunResult run={{ ...base, reconciliation: { ...base.reconciliation!, late_rows: 3, late_rows_reason: null } }} />);
    expect(lateStat().textContent).toMatch(/^Late rows3/);
    expect(lateStat().getAttribute("data-attention")).toBe("true");
    unmount();
    render(<DryRunResult run={{ ...base, reconciliation: { ...base.reconciliation!, late_rows: null,
      late_rows_reason: "the pipeline declares no incremental watermark" } }} />);
    expect(lateStat().textContent).toMatch(/not measured/);
    expect(lateStat().textContent).toMatch(/declares no incremental watermark/);
    expect(lateStat().textContent).not.toMatch(/\b0\b/);
  });
});
