/**
 * (h) Reconnect and stale edits on the real API (P4-07): reloading an investigation mid-run restarts its
 * live stream, which picks up where the run is; a dropped connection shows the retry state and recovers;
 * an edit made against an old version (another tab saved first) is refused with 412, nothing is
 * overwritten, and the person compares and reloads.
 */
import { ANALYST, badge, expect, LIVE, readState, requireLive, shot, signIn, test } from "./live";

test.describe("reconnect and stale edits (P4-07)", () => {
  requireLive();

  test("(h) reload mid-run: the stream restarts and the run goes on; a dropped connection shows its retry state", async ({ page, context }) => {
    const { ws } = readState();
    await signIn(page, ANALYST, `/w/${ws}`);
    await page.getByRole("button", { name: "Start work" }).first().click();
    await page.getByRole("dialog", { name: "Start work" }).getByRole("button", { name: /^Compare/ }).click();
    const form = page.getByRole("form", { name: "Start compare" });
    await form.getByLabel("What should the investigation find out?").fill("Compare P1 resolution hours across assignment groups");
    // with more than one ready source (the engineer's files), the person chooses which one to read
    if (await form.getByLabel("Data", { exact: true }).count()) await form.getByLabel("Data", { exact: true }).selectOption({ label: "ServiceNow" });
    await form.getByRole("button", { name: "Start investigation" }).click();
    await expect(page).toHaveURL(/\/work\/investigations\/run_[a-z0-9]+$/);
    const stream = page.locator("main").getByRole("status").filter({ hasText: /^(Live|Connecting|Reconnecting|Stream closed)/ });
    await expect(stream).toHaveText(/^Live/);
    const events = page.getByRole("tab", { name: /^Live events \(\d+\)/ });
    const count = async () => Number((await events.innerText()).match(/\((\d+)\)/)![1]);

    // reload mid-run: the page rebuilds from the API and the stream starts again
    await expect.poll(count).toBeGreaterThan(3);
    const before = await count();
    await page.reload();
    await expect(stream).toHaveText(/^Live/);
    await expect.poll(count, { timeout: 60_000 }).toBeGreaterThanOrEqual(before);

    // the network to the stream fails (a fault injected in the browser, not a mocked answer): the indicator
    // shows its retry state; once the network is back, "Reconnect now" opens the stream again
    const cut = async (route: import("@playwright/test").Route) => route.abort("connectionreset");
    await context.route("**/analysis/*/events*", cut);
    await page.reload();
    await expect(stream).toHaveText(/^Reconnecting \(attempt \d+ · retry in \d+ s/, { timeout: 30_000 });
    await shot(page, "h1-reconnecting");
    await context.unroute("**/analysis/*/events*", cut);
    const again = stream.getByRole("button", { name: "Reconnect now" });
    if (await again.isVisible()) await again.click();
    await expect(stream).toHaveText(/^(Live|Stream closed)/, { timeout: 60_000 });
    await expect(badge(page.locator("main"), "WAITING_USER").or(badge(page.locator("main"), "COMPLETED")).first())
      .toBeVisible({ timeout: 300_000 });
    expect(await count()).toBeGreaterThan(before);
  });

  test("(h) stale edit: another tab saved the step first → 412, nothing overwritten, compare, reload", async ({ page, browser }) => {
    const { ws, run } = readState();
    await signIn(page, ANALYST, `/w/${ws}/work?tab=thread&container=run%3A${run}`);
    const thread = page.getByRole("list", { name: "Data Thread: main" });
    const record = page.getByRole("button", { name: "Record this investigation as steps" });
    await expect(record.or(thread)).toBeVisible(); // recorded by the Data Thread journey, or recorded here
    if (await record.isVisible()) await record.click();
    const step =thread.locator("li.step-card", { has: page.getByRole("heading", { name: /^H-\d+:/ }) }).nth(2);
    const title = (await step.getByRole("heading", { level: 3 }).innerText()).trim();
    await step.getByRole("button", { name: /^Edit/ }).click();
    const mine = step.getByLabel("Spec (JSON)");
    const body = JSON.parse(await mine.inputValue()) as { analysis_spec: { min_group_size: number } };

    // the same analyst, another tab: saves a new version of the same step first
    const other = await browser.newContext({ baseURL: LIVE, viewport: { width: 1400, height: 1000 } });
    const tab = await other.newPage();
    await signIn(tab, ANALYST, `/w/${ws}/work?tab=thread&container=run%3A${run}`);
    const theirs = tab.getByRole("list", { name: "Data Thread: main" }).locator("li.step-card", { has: tab.getByRole("heading", { name: title, exact: true }) });
    await theirs.getByRole("button", { name: /^Edit/ }).click();
    await theirs.getByLabel("Spec (JSON)").fill(JSON.stringify({ ...body, analysis_spec: { ...body.analysis_spec, min_group_size: 40 } }, null, 2));
    await theirs.getByRole("button", { name: "Save and re-run" }).click();
    await expect(tab.getByText(/is now version \d+/)).toBeVisible({ timeout: 120_000 });
    await other.close();

    // this tab still has the old version open: its save is refused, nothing is overwritten
    await mine.fill(JSON.stringify({ ...body, analysis_spec: { ...body.analysis_spec, min_group_size: 60 } }, null, 2));
    await step.getByRole("button", { name: "Save and re-run" }).click();
    const stale = step.getByRole("alert").filter({ hasText: "Someone changed this." });
    await expect(stale).toBeVisible();
    await expect(stale).toContainText("nothing was overwritten");
    await stale.getByRole("button", { name: "Compare with mine" }).click();
    await expect(stale.getByRole("table")).toContainText("min_group_size");
    await shot(page, "h2-stale-edit");
    await stale.getByRole("button", { name: "Reload the current version" }).click();
    await expect(stale).toHaveCount(0);
    await expect(step.getByLabel("Spec (JSON)")).toHaveCount(0);
  });
});
