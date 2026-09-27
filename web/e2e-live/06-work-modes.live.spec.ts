/**
 * (g) Workspace work modes and the workflow builder (P7-19), on the real API: an owner turns ML work off
 * after reviewing the playbook changes, Start work stops offering Predict (and says why), turning it on
 * again brings it back; a workflow is built from registered agents as a step list, validated, published
 * and run at its exact version.
 */
import type { Page } from "@playwright/test";
import { ADMIN, badge, expect, readState, requireLive, shot, signIn, test } from "./live";

async function setModes(page: Page, wsName: string, ml: boolean): Promise<void> {
  await page.goto("/");
  const card = page.locator("article, section, li, div.card").filter({ has: page.getByRole("heading", { name: wsName, level: 2 }) }).first();
  await card.getByRole("button", { name: "Work modes" }).click();
  const settings = page.locator("section, div.card").filter({ has: page.getByRole("heading", { name: "Workspace work modes" }) }).last();
  const box = settings.getByRole("checkbox", { name: /ML and experiments/ });
  await (ml ? box.check() : box.uncheck());
  await settings.getByRole("button", { name: "Review changes" }).click();
  await expect(settings.getByRole("status")).toContainText(/\d+ playbook settings will change/);
  await expect(settings.getByRole("status")).toContainText(`playbook.train: ${ml ? "enable" : "disable"}`);
  await settings.getByRole("button", { name: "Save work modes" }).click();
  await expect(settings.getByRole("button", { name: "Save work modes" })).toHaveCount(0);
}

test.describe("work modes and the workflow builder (P7-19)", () => {
  requireLive();

  test("(g) changing work modes changes Start work", async ({ page }) => {
    const { ws, wsName } = readState();
    await signIn(page, ADMIN, "/");
    await setModes(page, wsName!, false);
    await page.goto(`/w/${ws}/work`);
    await page.getByRole("button", { name: "Start work" }).click();
    const kinds = page.getByRole("list", { name: "Job kinds" });
    await expect(kinds.getByRole("list", { name: "Why Predict cannot start" })).toContainText("ml work is not selected for this workspace");
    await expect(kinds.getByRole("button", { name: /^Predict/ })).toHaveCount(0);
    await expect(kinds.getByRole("button", { name: /^Explain/ })).toBeVisible();
    await shot(page, "g1-start-work-without-ml");
    await page.keyboard.press("Escape");

    await setModes(page, wsName!, true);
    await page.goto(`/w/${ws}/work`);
    await page.getByRole("button", { name: "Start work" }).click();
    await expect(page.getByRole("list", { name: "Job kinds" }).getByRole("list", { name: "Why Predict cannot start" })).toHaveCount(0);
    await expect(page.getByRole("list", { name: "Job kinds" }).getByRole("button", { name: /^Predict/ })).toBeVisible();
  });

  test("(g) workflow builder: a step list from registered agents → draft → publish → run that exact version", async ({ page }) => {
    const { ws } = readState();
    const slug = `live_${Date.now().toString(36)}`;
    await signIn(page, ADMIN, `/w/${ws}/work?tab=workflows`);
    await page.getByRole("button", { name: "Build workflow" }).click();
    const form = page.getByRole("form", { name: "Workflow builder" });
    await form.getByLabel("Title").first().fill("Live weekly SLA check");
    await form.getByLabel("Key", { exact: true }).fill(slug);
    await form.getByLabel("Purpose").fill("Profile the incident data and report what changed this week.");
    const step1 = form.getByRole("group", { name: "Step 1" });
    await step1.getByLabel("Step key").fill("context");
    await step1.getByLabel("Title").fill("Load the context");
    const agent = step1.getByLabel("Agent");
    const options = await agent.locator("option").evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value).filter(Boolean));
    expect(options.length).toBeGreaterThan(0);
    await agent.selectOption(options.find((o) => /context/.test(o)) ?? options[0]);
    await form.getByRole("button", { name: "Add step" }).click();
    const step2 = form.getByRole("group", { name: "Step 2" });
    await step2.getByLabel("Step key").fill("profile");
    await step2.getByLabel("Title").fill("Profile the tables");
    await step2.getByLabel("Agent").selectOption(options.find((o) => /profil/.test(o)) ?? options[options.length - 1]);
    await step2.getByLabel("Wait for").selectOption({ label: "Load the context" });
    await expect(form.getByRole("status")).toHaveCount(0);
    await form.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByText(/Draft v1 saved and validated\./)).toBeVisible();
    await page.getByRole("button", { name: "Publish version" }).click();
    const list = page.getByRole("list", { name: "Workflows" });
    const row = list.getByRole("listitem").filter({ hasText: `playbook.${slug}` });
    await expect(row).toContainText("v1");
    await shot(page, "g2-workflow-published");
    await row.getByRole("button", { name: "Run" }).click();
    const run = page.getByRole("form", { name: "Run workflow" });
    await expect(run).toContainText("Live weekly SLA check · v1");
    await run.getByLabel("Goal").fill("What changed in P1 incident volume this week?");
    if (await run.getByLabel("Data source").inputValue() === "") await run.getByLabel("Data source").selectOption({ label: "ServiceNow" });
    await run.getByRole("button", { name: "Start workflow" }).click();
    await expect(page).toHaveURL(/\/work\/investigations\/run_[a-z0-9]+$/);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("What changed in P1 incident volume this week?");
    await expect(badge(page.locator("main"), "COMPLETED").first()).toBeVisible({ timeout: 180_000 });
  });
});
