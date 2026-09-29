import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { session, type CitationsResponse } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { EvidenceCitations } from "../components/Citations";
import { conflictText, isCitations, numberGroups } from "../lib/citations";
import { INSIGHT, mockBackend, RUN, USER, WS } from "./mockBackend";

vi.setConfig({ testTimeout: 20000 });

const CITATIONS: CitationsResponse = {
  version: "citations.v1", subject_type: "insight", subject_id: INSIGHT, recorded: true,
  quantitative: [{ kind: "quantitative", id: "q:qry_1", label: "primary", query_id: "qry_1", result_hash: "9f86d081884c7d65",
    query_hash: null, step: null, fact_ids: ["f_top"], values: [0.45], verified: true }],
  documents: [{ kind: "document", id: "d:kdoc_sla#targets", document_id: "kdoc_sla", path: "rules/sla.md", anchor: "targets",
    heading: "SLA targets", pack: "itsm", section: "business_rules", document_sha256: "d".repeat(64), section_sha256: "5".repeat(64),
    excerpt: "The resolution rate for Network P1 incidents is 90%.", trusted: true,
    numbers: [{ text: "90%", value: 90, unit: "percent", sentence: "The resolution rate for Network P1 incidents is 90%.", source: "document", verified: false }] }],
  narrative_numbers: [{ text: "45%", source: "quantitative", citation_id: "q:qry_1" }, { text: "90%", source: "document", citation_id: "d:kdoc_sla#targets" }],
  conflicts: [{ document_citation_id: "d:kdoc_sla#targets", path: "rules/sla.md", anchor: "targets", fact_id: "f_top", metric: "resolution rate",
    subject: "Network", document_text: "90%", document_value: 90, measured_value: 0.45, unit: "fraction", relative_difference: 1,
    sentence: "The resolution rate for Network P1 incidents is 90%.", resolution: "measured_data_wins" }],
  summary: { quantitative: 1, documents: 1, conflicts: 1, document_sourced_numbers: 1, unbound_numbers: 0 },
};

async function axeClean(container: HTMLElement) {
  const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`);
}

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("citation helpers", () => {
  it("groups the text's numbers by source; only measured ones are verified", () => {
    expect(numberGroups(CITATIONS.narrative_numbers)).toEqual({ measured: ["45%"], document: ["90%"], unbound: [] });
  });
  it("states a conflict with the measured value winning", () => {
    expect(conflictText(CITATIONS.conflicts[0])).toBe(
      "rules/sla.md#targets states 90% for resolution rate (Network); measured 45%. The measured value is used.");
  });
  it("reads anything that is not a citations document as none", () => {
    expect(isCitations([])).toBe(false);
    expect(isCitations(null)).toBe(false);
    expect(isCitations(CITATIONS)).toBe(true);
  });
});

describe("EvidenceCitations", () => {
  it("lists measured and document evidence separately and flags the conflict", async () => {
    const { container } = render(<MemoryRouter><EvidenceCitations data={CITATIONS} workspaceId={WS} /></MemoryRouter>);
    const measured = screen.getByRole("list", { name: "Measured evidence" });
    expect(within(measured).getByText(/result hash/).textContent).toMatch(/9f86d081884c/);
    const docs = screen.getByRole("list", { name: "Document evidence" });
    expect(within(docs).getByText("rules/sla.md#targets")).toBeTruthy();
    expect(within(docs).getByText(/Document states: 90%/)).toBeTruthy();
    expect(within(docs).getByText("not verified")).toBeTruthy();
    expect(within(measured).queryByText(/90%/)).toBeNull(); // a document number is never listed as measured
    expect(screen.getByRole("list", { name: "Document and data conflicts" }).textContent).toMatch(/measured 45%/);
    expect(screen.getByLabelText("Numbers in the text by source").textContent).toMatch(/From a document, not verified: 90%/);
    expect(await axeClean(container)).toEqual([]);
  });
});

describe("Why trust this", () => {
  it("shows the finding's citations in the drawer", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (String(input).endsWith(`/insights/${INSIGHT}/citations`)) {
        return new Response(JSON.stringify(CITATIONS), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      const r = mockBackend(method, String(input), typeof init?.body === "string" ? init.body : null);
      return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
    });
    render(<AuthProvider><MemoryRouter initialEntries={[`/w/${WS}/investigate/${RUN}`]}><AppRoutes /></MemoryRouter></AuthProvider>);
    const f1 = await screen.findByRole("article", { name: "Finding F1" });
    fireEvent.click(within(f1).getByRole("button", { name: "Why trust this" }));
    const dialog = screen.getByRole("dialog", { name: "Why trust F1?" });
    expect(await within(dialog).findByRole("list", { name: "Measured evidence" })).toBeTruthy();
    expect(within(dialog).getByRole("list", { name: "Document evidence" })).toBeTruthy();
    expect(within(dialog).getByRole("list", { name: "Document and data conflicts" })).toBeTruthy();
  });
});
