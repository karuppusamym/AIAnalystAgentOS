/**
 * (e) Engineer, through the real UI and API (P6-03, P7-19): a file source from an upload, a file loaded
 * in Work → Prepare data, a recipe previewed and published, a writer destination allowed, a pipeline
 * saved and published, a dry run with its rows ledger, approve → materialize, a second version, and a
 * rollback to the first. The workspace owner is the engineer here: allowing a destination and rolling
 * back are owner actions; the approver decides the materializations in their own session.
 */
import type { Page } from "@playwright/test";
import { ADMIN, approveAs, expect, readState, requireLive, shot, signIn, test } from "./live";

type Row = (string | number)[];
const csv = (rows: Row[]) => Buffer.from(`order_id,region,amount\n${rows.map((r) => r.join(",")).join("\n")}\n`);
const COLS = [{ name: "order_id", type: "bigint" }, { name: "region", type: "text" }, { name: "amount", type: "double precision" }];
const OUT = [...COLS, { name: "large", type: "boolean" }];

async function loadFile(page: Page, source: string, name: string, rows: Row[], mode: "replace" | "append"): Promise<string> {
  const load = page.getByRole("form", { name: "Load a file" });
  if (await load.getByLabel("Into").count()) await load.getByLabel("Into").selectOption({ label: source });
  await load.getByLabel("File").setInputFiles({ name, mimeType: "text/csv", buffer: csv(rows) });
  await load.getByLabel("Table name").fill("orders");
  await load.getByLabel("If the table exists").selectOption(mode);
  await load.getByRole("button", { name: "Load file" }).click();
  const done = load.getByText(new RegExp(`^Loaded into src_[a-z0-9_]+\\.orders \\(${mode}\\)\\.$`));
  await expect(done).toBeVisible();
  return (await done.innerText()).match(/Loaded into ([a-z0-9_]+)\.orders/)![1];
}

async function dryRunAndMaterialize(page: Page, ws: string, pipeline: string, rows: number): Promise<void> {
  const detail = page.getByRole("region", { name: `Pipeline ${pipeline} v1` });
  const runs = detail.getByRole("region", { name: /^Dry run / });
  const previous = (await runs.count()) ? await runs.first().getAttribute("aria-label") : null;
  await detail.getByRole("button", { name: "Dry run" }).click();
  const run = previous ? detail.locator(`[role=region][aria-label^='Dry run ']:not([aria-label='${previous}'])`) : runs.first();
  await expect(run.getByText("Every check passed; writing it needs an approval.")).toBeVisible({ timeout: 60_000 });
  const ledger = run.getByRole("group", { name: "Rows" });
  await expect(ledger.locator(".stat", { hasText: "Input rows" })).toContainText(String(rows));
  await expect(ledger.locator(".stat", { hasText: "Output rows" })).toContainText(String(rows));
  await expect(ledger.locator(".stat", { hasText: "Quarantined rows" })).toContainText("0");
  await expect(ledger.locator(".stat", { hasText: "Rejected rows" })).toContainText("0");
  await expect(ledger.locator(".stat", { hasText: "Late rows" })).toBeVisible();
  await shot(page, "e2-dry-run-ledger");

  // nothing is written before an approval: request it, a pending approval is refused, the approver decides
  await detail.getByRole("button", { name: "Materialize this candidate" }).click();
  await expect(detail.getByRole("status", { name: /^Approval for writing/ })).toBeVisible();
  await detail.getByRole("button", { name: "Continue with the approved request" }).click();
  await expect(detail.getByRole("alert")).toContainText(/pending|approv/i);
  await approveAs(page.context().browser()!, ws, /Materialize a pipeline's output/);
  await detail.getByRole("button", { name: "Continue with the approved request" }).click();
  await expect(detail.getByText(new RegExp(`Promoted .*${pipeline}.* version \\d+ \\(${rows} rows\\)`))).toBeVisible();
}

test.describe("engineer: file → recipe → pipeline → dry run → approve → materialize → rollback (P6-03)", () => {
  requireLive();

  test("(e) engineer journey on the real API", async ({ page }) => {
    const { ws } = readState();
    // names unique per execution, so the journey can be re-run in the same workspace
    const id = Date.now().toString(36);
    const [files, recipeName, pipeline, dest] = [`Orders files ${id}`, `orders_clean_${id}`, `orders_mart_${id}`, `aos_live_${id}`];
    await signIn(page, ADMIN, `/w/${ws}/data/sources`);

    // a file source from an upload
    await page.getByRole("button", { name: "Add source" }).click();
    const add = page.getByRole("form", { name: "Add source" });
    await add.getByLabel("Kind", { exact: true }).selectOption("csv");
    await add.getByRole("textbox", { name: "Name *", exact: true }).fill(files);
    await add.getByLabel("Upload a file").setInputFiles({ name: "seed.csv", mimeType: "text/csv", buffer: csv([[1, "north", 10.5]]) });
    await add.getByRole("button", { name: "Add source" }).click();
    await expect(page.getByRole("heading", { name: new RegExp(`^${files} · csv`) })).toBeVisible();

    // Start work → Prepare data → load a file
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Work", exact: true }).click();
    await page.getByRole("button", { name: "Start work" }).click();
    await page.getByRole("list", { name: "Job kinds" }).getByRole("button", { name: /Prepare data/ }).click();
    await expect(page).toHaveURL(`/w/${ws}/work?tab=prepare`);
    const schema = await loadFile(page, files, "orders.csv", [[1, "north", 10.5], [2, "south", 20], [3, "north", 7.25], [4, "east", 99]], "replace");

    // a recipe: saved, previewed (nothing written), published
    await page.getByText("Advanced: new recipe from a specification").click();
    const recipe = { kind: "Recipe", name: recipeName, nodes: [
      { op: "source", id: "src", asset: `${schema}.orders`, schema: COLS },
      { op: "derive", id: "flagged", input: "src", columns: [{ name: "large", expr: "amount >= 50", type: "boolean" }] },
      { op: "output", id: "out", input: "flagged", name: "clean", keys: ["order_id"], grain: ["order_id"], schema: OUT }] };
    const nr = page.getByRole("form", { name: "New recipe" });
    await nr.getByLabel("Recipe specification (JSON)").fill(JSON.stringify(recipe));
    await nr.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByRole("heading", { name: `${recipeName} v1` })).toBeVisible();
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    await expect(page.getByText(/Preview — nothing was written/)).toBeVisible();
    await expect(page.getByRole("table", { name: "Preview of clean" })).toContainText("east");
    await page.getByRole("button", { name: "Publish", exact: true }).click();
    await expect(page.getByRole("list", { name: "Recipes" }).getByRole("button", { name: new RegExp(`^${recipeName} v1 published`) })).toBeVisible();

    // the owner allows a destination, then a pipeline is saved and published
    const pipes = page.getByRole("region", { name: "Pipelines" });
    await pipes.getByText(/^Destinations the writer may use/).click();
    const allow = pipes.getByRole("form", { name: "Allow a destination" });
    await allow.getByLabel("Schema").fill(dest);
    await allow.getByLabel("Tables").fill(pipeline);
    await allow.getByRole("button", { name: "Allow destination" }).click();
    await expect(pipes.getByRole("list", { name: "Writer destinations" })).toContainText(dest);
    await pipes.getByText("Advanced: new pipeline from a specification").click();
    const np = pipes.getByRole("form", { name: "New pipeline" });
    await np.getByLabel("Pipeline specification (JSON)").fill(JSON.stringify({ type: "pipeline", name: pipeline,
      recipes: [{ name: recipeName }], output: { output: "clean", keys: ["order_id"], grain: ["order_id"], schema: OUT },
      destination: { schema: dest, table: pipeline } }));
    await np.getByRole("button", { name: "Save draft" }).click();
    const detail = page.getByRole("region", { name: `Pipeline ${pipeline} v1` });
    await expect(detail.getByRole("list", { name: "Source to output" })).toContainText(`${dest}.${pipeline}`);
    await expect(detail.getByRole("button", { name: "Dry run" })).toBeDisabled();
    await detail.getByRole("button", { name: "Publish" }).click();
    await expect(detail.getByRole("button", { name: "Dry run" })).toBeEnabled();
    await shot(page, "e1-pipeline");

    await dryRunAndMaterialize(page, ws!, pipeline, 4);

    // a second version from appended rows, then roll back to the first
    await loadFile(page, files, "more.csv", [[5, "west", 12]], "append");
    await dryRunAndMaterialize(page, ws!, pipeline, 5);
    await detail.getByRole("link", { name: "Outputs → Managed tables" }).click();
    await expect(page).toHaveURL(`/w/${ws}/outputs?type=table&table=${pipeline}`);
    const table = page.getByRole("region", { name: `Table ${dest}.${pipeline}` });
    await expect(table).toContainText("v2");
    page.once("dialog", (d) => void d.accept());
    await table.getByRole("button", { name: "Roll back v2" }).click();
    await expect(page.getByText(/serves version 1 again; version 2 is kept as rolled back/)).toBeVisible();
    await shot(page, "e3-rolled-back");
  });
});
