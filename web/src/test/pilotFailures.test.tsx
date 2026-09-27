/**
 * P4-09 UI: a failed run, a failed schedule and a failed connector each show their cause where the person looks
 * (the investigation, the schedule's recent runs, the source card), and pilot readiness lists what is missing and
 * where, with the owners editable in place.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { session } from "../api";
import { ownersBody } from "../components/PilotReadiness";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { mockBackend, resetMockState, RUN, USER, WS } from "./mockBackend";

const CAUSE = "File source path does not exist: tickets";
const base = (path: string) => JSON.parse(mockBackend("GET", `http://x${path}`).body as string);

const READINESS = {
  workspace_id: WS, workspace_name: "ITSM", verdict: "not_ready", missing: 2, failing: 1, generated_at: "2026-09-27T10:00:00Z",
  checks: [
    { check: "owner.business", subject: "workspace", status: "missing", reason: "no named business owner",
      remediation: `a workspace owner names one: PUT /api/workspaces/${WS}/owners`, evidence: null },
    { check: "owner.technical", subject: "workspace", status: "pass", reason: "Tom Platform <tom@pilot.test>", remediation: null, evidence: null },
    { check: "connector.certified", subject: "source:src_sn (ServiceNow)", status: "missing",
      reason: "servicenow has catalog and unit tests only, no live certification", remediation: "run scripts/certify_connectors.py", evidence: null },
    { check: "connector.health", subject: "source:src_sn (ServiceNow)", status: "fail", reason: `the last connection failed: ${CAUSE}`,
      remediation: "fix the connection and discover the source again", evidence: null },
  ],
};

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("observable failures (P4-09)", () => {
  it("a failed connector shows its cause on the source card", async () => {
    mockFetch((m, p) => {
      if (m === "GET" && p === `/api/workspaces/${WS}/sources`) {
        const [src] = base(p);
        return { status: 200, body: [{ ...src, status: "error", last_error: CAUSE }] };
      }
      return null;
    });
    renderAt(`/w/${WS}/data/sources`);
    expect(await screen.findByText(CAUSE)).toBeTruthy();
  });

  it("a failed investigation shows why it failed", async () => {
    mockFetch((m, p) => {
      if (m === "GET" && p === `/api/workspaces/${WS}/analysis/${RUN}`) {
        return { status: 200, body: { ...base(p), status: "FAILED", error: "required task(s) failed: profile" } };
      }
      return null;
    });
    renderAt(`/w/${WS}/work/investigations/${RUN}`);
    expect(await screen.findByText(/required task\(s\) failed: profile/)).toBeTruthy();
    expect(screen.getByText(/Investigation error:/)).toBeTruthy();
  });

  it("a failed schedule run shows its error in the recent runs", async () => {
    mockFetch((m, p) => {
      if (m === "GET" && p === `/api/workspaces/${WS}/schedules`) {
        const [sch] = base(p);
        return { status: 200, body: [{ ...sch, recent_runs: [{ id: "srun_1", schedule_id: sch.id, status: "failed", trigger: "cron",
          scheduled_for: "2026-09-27T06:00:00Z", started_at: "2026-09-27T06:00:01Z", finished_at: "2026-09-27T06:00:02Z", result: {},
          error: `invalid_input: every crawl failed: connection failed: ${CAUSE}` }] }] };
      }
      return null;
    });
    renderAt(`/w/${WS}/operate/schedules`);
    expect(await screen.findByText(new RegExp(`every crawl failed: connection failed: ${CAUSE}`))).toBeTruthy();
  });
});

describe("pilot readiness (P4-09)", () => {
  it("lists every gap where it is and saves named owners", async () => {
    const f = mockFetch((m, p, body) => {
      if (m === "GET" && p === `/api/workspaces/${WS}/pilot-readiness`) return { status: 200, body: READINESS };
      if (m === "PUT" && p === `/api/workspaces/${WS}/owners`) return { status: 200, body: { workspace_id: WS, owners: JSON.parse(body ?? "{}") } };
      return null;
    });
    renderAt(`/w/${WS}/settings/policy`);
    const card = await screen.findByRole("region", { name: "Pilot readiness" });
    expect(within(card).getByText("3 to resolve")).toBeTruthy();
    const table = within(card).getByRole("table", { name: "Pilot readiness checks" });
    expect(within(table).getByText(`the last connection failed: ${CAUSE}`)).toBeTruthy();
    expect(within(table).getByText("no named business owner")).toBeTruthy();
    fireEvent.click(within(card).getByText("Name the owners"));
    const form = within(card).getByRole("form", { name: "Owners of this workspace" });
    fireEvent.change(within(form).getByLabelText("Business owner"), { target: { value: "Priya Service Owner" } });
    fireEvent.change(within(form).getByLabelText("Business owner email"), { target: { value: "priya@pilot.test" } });
    fireEvent.click(within(form).getByRole("button", { name: /Save owners of this workspace/ }));
    await waitFor(() => expect(calls(f, "PUT", /\/owners$/)).toHaveLength(1));
    const [[, init]] = calls(f, "PUT", /\/owners$/);
    expect(bodyOf(init)).toEqual({ business: { name: "Priya Service Owner", email: "priya@pilot.test" }, technical: null });
  });

  it("validates owners before sending", () => {
    expect(ownersBody({ businessName: "Priya", businessEmail: "not-an-address" }).problems).toEqual(["The business owner needs an email address."]);
    expect(ownersBody({ technicalEmail: "t@x.test" }).problems).toEqual(["Name the technical owner."]);
    expect(ownersBody({}).body).toEqual({ business: null, technical: null });
  });
});
