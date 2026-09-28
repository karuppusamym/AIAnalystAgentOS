/**
 * After the answer: completing an approved "Add to dashboard" from the thread, a schedule request that
 * survives a reload, "Save as report", "Why these numbers?" over the API, actions gated by the caller's
 * workspace role, Re-verify on a void finding, report formats that were not generated, and schedules
 * whose kind the form cannot edit.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { session, type Artifact, type AskPromotion } from "../api";
import { reverifyText } from "../components/WhyNumber";
import { canComplete, mergePromotion, promotionText } from "../lib/ask";
import { scheduleEditable } from "../lib/schedules";
import { ReportRow, unavailableFormats } from "../pages/Reports";
import { scheduleRequest } from "../pages/Ask";
import { needsRole } from "../routes";
import { bodyOf, calls, mockFetch, renderAt, type Handler } from "./harness";
import { askTurn, resetMockState, THREAD_OLD, USER, WORKSPACE, WS } from "./mockBackend";
import { INSIGHT_VOID_ID } from "./mockWave1";

vi.setConfig({ testTimeout: 20000 });

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

const PENDING: AskPromotion = { target: "dashboard", id: "apr_d1", status: "approval_required", approval_id: "apr_d1", dashboard: "Ask answers",
  approval_status: "approved", at: "2026-09-27T00:00:00Z" };

/** The old thread's turn with these promotions; the workspace with this role. */
function world(promotions: AskPromotion[], role = "owner", extra?: Handler): Handler {
  return (m, p, body, url) => {
    if (m === "GET" && p === `/api/workspaces/${WS}`) return { status: 200, body: { ...WORKSPACE, role } };
    if (m === "GET" && p === `/api/ask/threads/${THREAD_OLD}`) {
      return { status: 200, body: { id: THREAD_OLD, workspace_id: WS, user_id: USER.id, title: "Weekly P1 volume", archived: false,
        created_at: "2026-09-27T00:00:00Z", updated_at: "2026-09-27T00:00:00Z",
        turns: [askTurn("askt_old", "How many P1 incidents per week?", { thread_id: THREAD_OLD, promotions })] } };
    }
    return extra?.(m, p, body, url) ?? null;
  };
}

describe("helpers", () => {
  it("words each promotion state and completes only an approved one", () => {
    expect(promotionText({ ...PENDING, approval_status: "pending" })).toMatch(/Waiting for approval before the chart is added to Ask answers/);
    expect(promotionText(PENDING)).toMatch(/Approved: complete it/);
    expect(promotionText({ ...PENDING, approval_status: "rejected" })).toMatch(/approval rejected\. Request it again/);
    expect(promotionText({ target: "dashboard", id: "art", status: "published", destination: "preview", dashboard: "Ask answers" }))
      .toMatch(/in-platform preview/);
    expect(promotionText({ target: "report", id: "art_r", status: "created" })).toMatch(/Saved as a report/);
    expect(canComplete(PENDING)).toBe(true);
    expect(canComplete({ ...PENDING, approval_status: "expired" })).toBe(false);
  });

  it("completing a promotion replaces its pending entry", () => {
    const done: AskPromotion = { target: "dashboard", id: "art_1", status: "published", approval_id: "apr_d1" };
    expect(mergePromotion([{ target: "metric", id: "m", status: "proposed" }, PENDING], done).map((p) => p.status)).toEqual(["proposed", "published"]);
    expect(mergePromotion([PENDING], { target: "monitor", id: "mon", status: "created" })).toHaveLength(2);
  });

  it("says which role an action needs", () => {
    expect(needsRole("owner", "editor", "X")).toBeNull();
    expect(needsRole("analyst", "editor", "Training a model")).toBe("Training a model needs the editor role in this workspace (yours: analyst).");
  });

  it("keeps a schedule request unless it can no longer be used", () => {
    const req: AskPromotion = { target: "schedule", id: "apr_s", status: "approval_required", approval_id: "apr_s", approval_status: "pending" };
    expect(scheduleRequest(askTurn("t", "q", { promotions: [req] }))?.approval_id).toBe("apr_s");
    expect(scheduleRequest(askTurn("t", "q", { promotions: [{ ...req, approval_status: "expired" }] }))).toBeNull();
  });

  it("only the re-analysis-style kinds are editable in the schedule form", () => {
    expect(scheduleEditable("reanalysis")).toBe(true);
    for (const k of ["saved_analysis", "step", "pipeline"]) expect(scheduleEditable(k)).toBe(false);
  });

  it("words a re-verification", () => {
    expect(reverifyText({ record_id: "r", subject_type: "insight", subject_id: "i", status: "started", run_id: "run_9" })).toMatch(/replay run run_9/);
    expect(reverifyText({ record_id: "r", subject_type: "step", subject_id: "s", status: "reverified" })).toMatch(/new verdict/);
  });
});

describe("Ask: after the answer", () => {
  it("completes an approved dashboard request from the thread and shows it published", async () => {
    const f = mockFetch(world([PENDING], "owner", (m, p) => (m === "POST" && p === "/api/ask/turns/askt_old/promote"
      ? { status: 200, body: { target: "dashboard", id: "art_c1", status: "published", approval_id: "apr_d1", dashboard: "Ask answers",
        destination: "preview", url: "preview://preview:ws:dashboard:ask_answers", publication_id: "pub_1", at: "2026-09-28T00:00:00Z" } }
      : null)));
    renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
    const promote = await screen.findByRole("region", { name: "Promote this answer" });
    expect(within(promote).getByText(/Approved: complete it to publish the chart to Ask answers/)).toBeTruthy();
    fireEvent.click(within(promote).getByRole("button", { name: "Complete: publish the chart" }));
    expect(await within(promote).findByText(/Published to Ask answers in the in-platform preview/)).toBeTruthy();
    expect(within(promote).queryByText(/Approved: complete it/)).toBeNull();
    expect(within(promote).getByRole("link", { name: "Open the chart" })).toBeTruthy();
    const [, init] = calls(f, "POST", /\/ask\/turns\/askt_old\/promote$/)[0];
    expect(bodyOf(init)).toEqual({ target: "dashboard", approval_id: "apr_d1" });
  });

  it("saves an answer as a report and links to it", async () => {
    const f = mockFetch(world([], "owner", (m, p) => (m === "POST" && p === "/api/ask/turns/askt_old/promote"
      ? { status: 200, body: { target: "report", id: "art_r1", status: "created", formats: ["html"], at: "2026-09-28T00:00:00Z" } } : null)));
    renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
    const promote = await screen.findByRole("region", { name: "Promote this answer" });
    fireEvent.click(within(promote).getByRole("button", { name: "Save as report" }));
    expect(await within(promote).findByText(/Saved as a report/)).toBeTruthy();
    expect(within(promote).getByRole("link", { name: "Open the report" }).getAttribute("href")).toMatch(/type=report.*artifact=art_r1/);
    expect(bodyOf(calls(f, "POST", /\/promote$/)[0][1])).toEqual({ target: "report" });
  });

  it("an approved schedule request survives a reload and is activated with its approval", async () => {
    const req: AskPromotion = { target: "schedule", id: "apr_s", status: "approval_required", approval_id: "apr_s", approval_status: "approved",
      cron: "0 9 * * 1", timezone: "UTC", expires_at: "2026-09-30T00:00:00Z" };
    const f = mockFetch(world([req], "owner", (m, p) => (m === "POST" && p === "/api/ask/turns/askt_old/schedule"
      ? { status: 200, body: { status: "created", id: "sch_9" } } : null)));
    renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
    expect(await screen.findByText(/Approved: activate it now/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Activate approved schedule" }));
    expect(await screen.findByText(/Calculation scheduled/)).toBeTruthy();
    expect(calls(f, "POST", /^\/api\/ask\/turns\/askt_old\/schedule$/)).toHaveLength(1); // one /api prefix, not two
    expect(bodyOf(calls(f, "POST", /\/schedule$/)[0][1])).toMatchObject({ approval_id: "apr_s", cron: "0 9 * * 1", timezone: "UTC" });
  });

  it("an analyst sees only what an analyst may do, with the reason", async () => {
    mockFetch(world([], "analyst"));
    renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
    const promote = await screen.findByRole("region", { name: "Promote this answer" });
    await within(promote).findByText(/Proposing a metric, a monitor or a dashboard chart needs the editor role/);
    for (const name of ["Propose as metric", "Monitor this", "Add to dashboard"]) expect(within(promote).queryByRole("button", { name })).toBeNull();
    for (const name of ["Save as verified query", "Save as report", "Investigate why"]) expect(within(promote).getByRole("button", { name })).toBeTruthy();
    expect(screen.getByText(/Scheduling a calculation needs the editor role/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Schedule this calculation" })).toBeNull();
  });

  it("Why these numbers? traces each number through the API when opened", async () => {
    const f = mockFetch(world([], "owner", (m, p) => (m === "GET" && p === "/api/ask/turns/askt_old/why" ? { status: 200, body: {
      subject: { type: "ask_turn", id: "askt_old" }, state: "unknown",
      verification_state: { state: null, badge: "unverified", record_id: null, void: null },
      numbers: [{ text: "292", value: 292, state: "unknown", links: [
        { link: "fact", state: "ok", detail: { value: 292 } },
        { link: "verdict", state: "unknown", reason: "no verification record: record the thread as steps" }] }],
    } } : null)));
    renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
    const turn = await screen.findByRole("article", { name: "Question 1" });
    expect(calls(f, "GET", /\/why$/)).toHaveLength(0);
    const details = within(turn).getByText("Why these numbers?").closest("details") as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    const trail = await within(turn).findByRole("region", { name: "Number 292" });
    expect(within(trail).getByText(/no verification record/)).toBeTruthy();
    expect(calls(f, "GET", /\/ask\/turns\/askt_old\/why$/)).toHaveLength(1);
  });
});

describe("Re-verify, reports and schedules", () => {
  it("a void finding can be re-verified from its page", async () => {
    const f = mockFetch((m, p) => (m === "POST" && p === "/api/verification/ver_3/reverify"
      ? { status: 200, body: { record_id: "ver_3", subject_type: "insight", subject_id: INSIGHT_VOID_ID, status: "started", run_id: "run_replay" } }
      : null));
    renderAt(`/w/${WS}/outputs/findings/${INSIGHT_VOID_ID}`);
    fireEvent.click(await screen.findByRole("button", { name: "Re-verify" }));
    expect(await screen.findByText(/Re-verification started: replay run run_replay/)).toBeTruthy();
    expect(calls(f, "POST", /\/verification\/ver_3\/reverify$/)).toHaveLength(1);
  });

  it("a report row says which formats were not generated and why, and links an Ask answer to its question", () => {
    const report: Artifact = {
      id: "art_1", workspace_id: WS, run_id: null, type: "report", name: "answer_askt_1", version: 1, status: "final",
      platform: null, external_id: null, external_url: null, creator_agent: null, creator_user: "u1",
      content: { kind: "ask_answer", title: "P1 by group", files: { html: { sha256: "y", bytes: 3, mime: "text/html", ext: "html" } },
        unavailable_formats: { pdf: "PDF reports need the reports extra (pip install analystos[reports])" },
        origin: { type: "ask", turn_id: "askt_1", thread_id: "ask_1" } },
      content_hash: "h", created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
    };
    expect(unavailableFormats(report)).toEqual([["pdf", "PDF reports need the reports extra (pip install analystos[reports])"]]);
    render(<MemoryRouter><ReportRow wsId={WS} report={report} /></MemoryRouter>);
    expect(screen.getByText(/PDF not generated: PDF reports need the reports extra/)).toBeTruthy();
    expect(screen.getByText("Ask answer")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Source question" }).getAttribute("href")).toMatch(/ask\?thread=ask_1$/);
    expect(screen.queryByRole("button", { name: "Download PDF" })).toBeNull();
  });

  it("a saved-analysis schedule offers no Edit form", async () => {
    mockFetch((m, p) => (m === "GET" && p === `/api/workspaces/${WS}/schedules` ? { status: 200, body: [
      { id: "sch_sa", workspace_id: WS, name: "P1 by group", kind: "saved_analysis", cron: "0 9 * * *", timezone: "UTC",
        config: { turn_id: "askt_1", fingerprint: "f", approval_id: "apr_s" }, enabled: true, owner_id: USER.id, next_run_at: null,
        last_run_at: null, created_at: "2026-09-01T00:00:00Z", recent_runs: [] },
      { id: "sch_1", workspace_id: WS, name: "Weekly re-analysis", kind: "reanalysis", cron: "0 7 * * 1", timezone: "UTC", config: {},
        enabled: true, owner_id: USER.id, next_run_at: null, last_run_at: null, created_at: "2026-09-01T00:00:00Z", recent_runs: [] },
    ] } : null));
    renderAt(`/w/${WS}/operate/schedules`);
    const saved = await screen.findByRole("region", { name: "Schedule P1 by group" });
    expect(within(saved).queryByRole("button", { name: "Edit" })).toBeNull();
    expect(within(saved).getByText("Set by its source")).toBeTruthy();
    const weekly = screen.getByRole("region", { name: "Schedule Weekly re-analysis" });
    expect(within(weekly).getByRole("button", { name: "Edit" })).toBeTruthy();
  });
});

describe("ML actions follow the editor role", () => {
  const ANALYST = { ...USER, is_admin: false };
  const asAnalyst: Handler = (m, p) => (m === "GET" && p === `/api/workspaces/${WS}` ? { status: 200, body: { ...WORKSPACE, role: "analyst" } }
    : m === "GET" && p === "/api/auth/me" ? { status: 200, body: ANALYST } : null);
  beforeEach(() => session.set("mock-token", ANALYST));

  it("an analyst cannot start training and is told why", async () => {
    mockFetch(asAnalyst);
    renderAt(`/w/${WS}/work?tab=experiments`);
    expect(await screen.findByText(/Training a model needs the editor role in this workspace \(yours: analyst\)/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "New prediction model" })).toBeNull();
  });

  it("an analyst sees models but not scoring or rollback", async () => {
    mockFetch(asAnalyst);
    renderAt(`/w/${WS}/outputs?type=model`);
    const model = await screen.findByRole("region", { name: "Model p1_breach" });
    await waitFor(() => expect(screen.getByText(/Scoring with a model or rolling it back needs the editor role/)).toBeTruthy());
    expect(within(model).queryByText(/Score approved data with/)).toBeNull();
    expect(within(model).queryByRole("button", { name: "Roll back to the previous champion" })).toBeNull();
  });
});
