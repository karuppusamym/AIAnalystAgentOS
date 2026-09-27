/**
 * Notebooks (P7-12) as a Work item: markdown, SQL and restricted-Python cells are steps; add, run,
 * edit (If-Match, dependents re-run), run all, and a failed version shown as failed with its error.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session } from "../api";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
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

async function addCell(type: "markdown" | "sql" | "python", source: string) {
  const form = await screen.findByRole("form", { name: "Add a cell" });
  fireEvent.change(within(form).getByLabelText("Cell type"), { target: { value: type } });
  fireEvent.change(within(form).getByLabelText("Source"), { target: { value: source } });
  fireEvent.click(within(form).getByRole("button", { name: "Add and run" }));
}

describe("notebooks (P7-12)", () => {
  it("adds markdown, SQL and Python cells, each run as a step", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work?tab=notebooks&notebook=nb_1`);
    expect(await screen.findByRole("tab", { name: "Notebooks", selected: true })).toBeTruthy();
    expect(await screen.findByText("No cells yet")).toBeTruthy();
    await addCell("markdown", "Notes on **P1** incidents");
    const md = await screen.findByRole("listitem", { name: "Cell 1 (Markdown)" });
    expect(within(md).getByText("P1").tagName).toBe("STRONG");
    await addCell("sql", "SELECT assignment_group, COUNT(*) AS incidents FROM stg_sn.incident GROUP BY 1");
    const sql = await screen.findByRole("listitem", { name: "Cell 2 (SQL)" });
    expect(await within(sql).findByRole("table", { name: /Result of/ })).toBeTruthy();
    expect(within(sql).getByText("verified")).toBeTruthy();
    await addCell("python", "result = sum(r['incidents'] for r in inputs['cell2'])");
    const py = await screen.findByRole("listitem", { name: "Cell 3 (Python)" });
    expect((await within(py).findByRole("table", { name: /Result of/ })).textContent).toMatch(/322/);
    expect(calls(f, "POST", /\/notebooks\/nb_1\/cells$/).map(([, i]) => bodyOf(i).cell)).toEqual(["markdown", "sql", "python"]);
  });

  it("an edit re-runs the cell and the cells that read it; a refused SQL edit is shown as a failed version", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work?tab=notebooks&notebook=nb_1`);
    await addCell("sql", "SELECT 1");
    await screen.findByRole("listitem", { name: "Cell 1 (SQL)" });
    await addCell("python", "result = 1");
    await screen.findByRole("listitem", { name: "Cell 2 (Python)" });

    const sql = screen.getByRole("listitem", { name: "Cell 1 (SQL)" });
    fireEvent.click(within(sql).getByRole("button", { name: /^Edit/ }));
    fireEvent.change(within(sql).getByLabelText("Source"), { target: { value: "SELECT 2" } });
    fireEvent.click(within(sql).getByRole("button", { name: "Save and run" }));
    expect(await screen.findByText(/Cell re-ran as version 2\. 1 cell that read it re-ran; 1 earlier verdict is now void\./)).toBeTruthy();
    const [[, init]] = calls(f, "PATCH", /\/notebooks\/nb_1\/cells\/cell_1$/);
    expect(new Headers(init!.headers).get("If-Match")).toBe('"1"');

    const again = await screen.findByRole("listitem", { name: "Cell 1 (SQL)" });
    fireEvent.click(within(again).getByRole("button", { name: /^Edit/ }));
    fireEvent.change(within(again).getByLabelText("Source"), { target: { value: "DELETE FROM stg_sn.incident" } });
    fireEvent.click(within(again).getByRole("button", { name: "Save and run" }));
    const failed = await screen.findByText(/Version 3 failed:/);
    expect(failed.closest("[role=alert]")!.textContent).toMatch(/refused by the query gateway: only SELECT statements are allowed/);
    expect(failed.closest("[role=alert]")!.textContent).toMatch(/Earlier versions and their results stay under Versions/);
    const cell = screen.getByRole("listitem", { name: "Cell 1 (SQL)" });
    fireEvent.click(within(cell).getByRole("button", { name: /^Versions \(3\)/ }));
    expect(await within(cell).findByRole("listitem", { name: "Version 2" })).toBeTruthy();
  });

  it("restricted Python: a disallowed import is refused before any cell exists; a runtime error is a failed version", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work?tab=notebooks&notebook=nb_1`);
    await addCell("python", "import os\nresult = os.listdir('/')");
    const form = screen.getByRole("form", { name: "Add a cell" });
    expect(await within(form).findByText(/refused by the sandbox policy: line 1: import of 'os' is not allowed/)).toBeTruthy();
    expect(screen.queryByRole("list", { name: "Cells" })).toBeNull();
    // the source stays in the form to fix
    expect((within(form).getByLabelText("Source") as HTMLTextAreaElement).value).toMatch(/import os/);
    await addCell("python", "result = 1/0");
    const py = await screen.findByRole("listitem", { name: "Cell 1 (Python)" });
    expect(within(py).getByText(/Version 1 failed:/).closest("[role=alert]")!.textContent).toMatch(/ZeroDivisionError/);
    fireEvent.click(screen.getByRole("button", { name: "Run all cells" }));
    expect(await screen.findByText(/Ran 1 cell in order on today's data\./)).toBeTruthy();
  });

  it("creates a notebook and opens it", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work?tab=notebooks`);
    const form = await screen.findByRole("form", { name: "New notebook" });
    fireEvent.change(within(form).getByLabelText("New notebook"), { target: { value: "Backlog study" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create notebook" }));
    expect(await screen.findByRole("region", { name: "Notebook Backlog study" })).toBeTruthy();
    expect(screen.getByTestId("location").textContent).toMatch(/notebook=nb_2/);
  });
});
