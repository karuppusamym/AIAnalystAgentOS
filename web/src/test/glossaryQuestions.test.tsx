/**
 * Stream E: glossary and description suggestions in the review queue. A scan queues them (the button shows the
 * counts), a skeleton cannot be accepted until a person writes the definition, Edit & accept posts the edited
 * body, a description question is answered in place, and the Overview counts the questions that need someone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { session, type KnowledgeSuggestion } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { evidenceLines, needsAnswer, scanSummary } from "../lib/knowledge";
import { meaningHint } from "../pages/WorkspaceHome";
import { mockBackend, resetMockState, USER, WS } from "./mockBackend";
import { SCAN_ABBR, SCAN_QUESTION, SCAN_TERM, seedScan } from "./mockKnowledge";

vi.setConfig({ testTimeout: 20000 });

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend((init?.method ?? "GET").toUpperCase(), String(input), typeof init?.body === "string" ? init.body : null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function calls(f: ReturnType<typeof mockFetch>, method: string, re: RegExp): [string, RequestInit][] {
  return (f.mock.calls as [string, RequestInit | undefined][])
    .filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u))).map(([u, i]) => [String(u), i ?? {}]);
}

const bodyOf = (init: RequestInit) => JSON.parse(String(init.body)) as Record<string, unknown>;

function renderAt(path: string) {
  return render(<AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter></AuthProvider>);
}

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

const suggestion = (kind: string, fields: KnowledgeSuggestion["fields"]): KnowledgeSuggestion => ({
  id: "s", kind, subject: "x", title: "X", path: "p.md", fields, confidence: 0.2, origin: "glossary.scan", proposed_by: "process:x",
  batch: null, status: "pending", decided_by: null, decided_at: null, reason: null, revision: null, created_at: "2026-09-27T00:00:00Z",
});
const f = (value: unknown, source = "rule") => ({ value, confidence: 0.5, provenance: { source } });

describe("glossary question helpers", () => {
  it("knows which drafts need a person's answer and words the evidence plainly", () => {
    expect(needsAnswer(suggestion("glossary_term", { body: f("1 = ?"), placeholder: f(true) }))).toBe(true);
    expect(needsAnswer(suggestion("glossary_term", { body: f("1 = Critical", "human"), placeholder: f(true) }))).toBe(false);
    expect(needsAnswer(suggestion("glossary_term", { body: f("SLA: Service Level Agreement."), placeholder: f(false) }))).toBe(false);
    expect(needsAnswer(suggestion("description_question", { description: f("U flag2.") }))).toBe(true);
    expect(needsAnswer(suggestion("description_question", { description: f("Lifecycle state.", "model") }))).toBe(false);
    expect(evidenceLines(suggestion("glossary_term", { evidence: f([
      { kind: "code_set", where: "incident.priority", detail: "5 values: 1–5" }, { kind: "ask", question: "how many P1 are open" }]) })))
      .toEqual(["found in incident.priority: 5 values: 1–5", "asked in Ask: “how many P1 are open”"]);
    expect(scanSummary({ glossary_terms: 1, description_questions: 2, candidates: 3, by_rule: {}, skipped_known: 4, skipped_decided: 0,
      assets: 2, model: { called: true, filled: 1 } }))
      .toBe("Found 1 new glossary suggestion and 2 questions about descriptions. 4 were already in your glossary. AI drafted 1 definition for you to check.");
    expect(meaningHint(3)).toBe("3 questions about your data need an answer");
    expect(meaningHint(1)).toBe("1 question about your data needs an answer");
  });
});

describe("Review queue: glossary suggestions and description questions", () => {
  it("scans, shows the evidence, refuses an unfilled skeleton, and Edit & accept posts the edited definition", async () => {
    const fetch = mockFetch();
    const { container } = renderAt(`/w/${WS}/knowledge/catalog?tab=review`);
    await screen.findByRole("list", { name: "Knowledge drafts" });
    fireEvent.click(screen.getByRole("button", { name: "Scan for glossary suggestions" }));
    expect(await screen.findByText(/Found 2 new glossary suggestions and 1 question about descriptions\. 3 were already in your glossary\./))
      .toBeTruthy();
    expect(calls(fetch, "POST", /knowledge\/glossary\/scan$/)).toHaveLength(1);

    const card = await screen.findByRole("article", { name: "Question: Priority" });
    expect(within(card).getByText("suggested glossary term")).toBeTruthy();
    expect(within(card).getByText("What do the Priority codes 1–5 mean?")).toBeTruthy();
    expect(within(card).getByText("found in incident.priority: 5 values: 1–5")).toBeTruthy();
    expect(within(card).getByText("asked in Ask: “how many P1 incidents breached”")).toBeTruthy();
    expect(within(card).getByText(/a draft: fill in the blanks/)).toBeTruthy();
    // a skeleton is not batch-selectable and cannot be accepted as it stands
    expect(within(card).queryByLabelText("Select Priority")).toBeNull();
    expect((within(card).getByRole("button", { name: "Accept" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByLabelText("Select CMDB")).toBeTruthy();
    const axeResult = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(axeResult.violations.map((v) => v.id)).toEqual([]);

    fireEvent.click(within(card).getByRole("button", { name: "Edit & accept" }));
    const form = within(card).getByRole("form", { name: "Edit Priority" });
    fireEvent.click(within(form).getByRole("button", { name: "Accept with edits" }));
    expect(await within(card).findByRole("alert")).toBeTruthy();  // nothing sent: the definition is still a skeleton
    expect(calls(fetch, "POST", /suggestions\/review$/)).toHaveLength(0);
    fireEvent.change(within(form).getByLabelText("Definition"), { target: { value: "1 = Critical, 2 = High, 3 = Moderate, 4 = Low, 5 = Planning." } });
    fireEvent.change(within(form).getByLabelText("Also called (comma-separated)"), { target: { value: "urgency level, P-level" } });
    fireEvent.click(within(form).getByRole("button", { name: "Accept with edits" }));
    await waitFor(() => expect(calls(fetch, "POST", /suggestions\/review$/)).toHaveLength(1));
    expect(bodyOf(calls(fetch, "POST", /suggestions\/review$/)[0][1])).toEqual({ decisions: [{ id: SCAN_TERM, action: "edit", fields: {
      body: "1 = Critical, 2 = High, 3 = Moderate, 4 = Low, 5 = Planning.", synonyms: ["urgency level", "P-level"] } }] });
    expect(await screen.findByText("glossary term ctx_new created")).toBeTruthy();
    expect(screen.queryByRole("article", { name: "Question: Priority" })).toBeNull();
  });

  it("answers a description question with the pre-filled guess edited, and rejects a term with a reason", async () => {
    const fetch = mockFetch();
    seedScan();
    renderAt(`/w/${WS}/knowledge/catalog?tab=review`);
    const q = await screen.findByRole("article", { name: "Question: Describe incident.u_flag2" });
    expect(within(q).getByText("What does “u_flag2” in Incident mean?")).toBeTruthy();
    const answer = within(q).getByLabelText("Your answer") as HTMLTextAreaElement;
    expect(answer.value).toBe("U flag2.");
    expect(within(q).getByText(/Pre-filled with the current best guess/)).toBeTruthy();
    fireEvent.change(answer, { target: { value: "Set when the caller is a VIP." } });
    fireEvent.click(within(q).getByRole("button", { name: "Save answer" }));
    await waitFor(() => expect(calls(fetch, "POST", /suggestions\/review$/)).toHaveLength(1));
    expect(bodyOf(calls(fetch, "POST", /suggestions\/review$/)[0][1])).toEqual({
      decisions: [{ id: SCAN_QUESTION, action: "edit", fields: { description: "Set when the caller is a VIP." } }] });

    const cmdb = await screen.findByRole("article", { name: "Question: CMDB" });
    fireEvent.click(within(cmdb).getByRole("button", { name: "Reject" }));
    fireEvent.click(within(cmdb).getAllByRole("button", { name: "Reject" }).at(-1)!);
    expect(await within(cmdb).findByRole("alert")).toBeTruthy();  // a reason is needed
    fireEvent.change(within(cmdb).getByLabelText("Why is this not a term here?"), { target: { value: "We say CI database" } });
    fireEvent.click(within(cmdb).getAllByRole("button", { name: "Reject" }).at(-1)!);
    await waitFor(() => expect(calls(fetch, "POST", /suggestions\/review$/)).toHaveLength(2));
    expect(bodyOf(calls(fetch, "POST", /suggestions\/review$/)[1][1])).toEqual({
      decisions: [{ id: SCAN_ABBR, action: "reject", reason: "We say CI database" }] });
  });
});

describe("Overview: questions about your data", () => {
  it("counts the glossary and description questions and links to the review queue", async () => {
    mockFetch();
    seedScan();
    renderAt(`/w/${WS}`);
    const card = (await screen.findByRole("heading", { name: "What needs you" })).closest("section")!;
    await waitFor(() => expect(within(card).getByText("Questions about your data").previousElementSibling?.textContent).toBe("3"));
    expect(within(card).getByText("3 questions about your data need an answer")).toBeTruthy();
    expect(within(card).getByText("Questions about your data").closest("a")?.getAttribute("href")).toBe(`/w/${WS}/data/catalog?tab=review`);
  });
});
