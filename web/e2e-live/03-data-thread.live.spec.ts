/**
 * (d) The Data Thread on the real API (P7-04, P7-05): the analyst records the investigation as steps,
 * edits a method step (the finding that reads it re-runs, its earlier verdict turns void, v1 stays
 * readable), pins a verified finding through an approval the approver decides in their own session,
 * forks a branch, compares it with main side by side and merges it into a report that keeps its lineage.
 */
import { ANALYST, approveAs, badge, expect, readState, requireLive, shot, signIn, test } from "./live";

test.describe("Data Thread (P7-04, P7-05)", () => {
  requireLive();

  test("(d) edit a step → dependents re-run and verdicts void → pin through an approval → fork → compare → merge", async ({ page, browser }) => {
    const { ws, run } = readState();
    await signIn(page, ANALYST, `/w/${ws}/work/investigations/${run}`);
    await page.getByRole("link", { name: "Data Thread" }).click();
    await expect(page).toHaveURL(`/w/${ws}/work?tab=thread&container=run%3A${run}`);
    await expect(page.getByLabel("Thread", { exact: true })).toHaveValue(`run:${run}`);
    const record = page.getByRole("button", { name: "Record this investigation as steps" });
    const thread = page.getByRole("list", { name: "Data Thread: main" });
    await expect(record.or(thread)).toBeVisible(); // a thread already recorded by an earlier execution shows its steps
    if (await record.isVisible()) await record.click();
    await expect(thread).toBeVisible();
    const steps = thread.getByRole("listitem").filter({ has: page.getByRole("heading", { level: 3 }) });
    expect(await steps.count()).toBeGreaterThan(5);

    // edit the first hypothesis's method (a larger minimum group): it re-runs, and so does the finding reading it
    const method = thread.locator("li.step-card", { has: page.getByRole("heading", { name: /^H-\d+:/ }) }).first();
    const methodTitle = (await method.getByRole("heading", { level: 3 }).innerText()).trim();
    await method.getByRole("button", { name: /^Edit/ }).click();
    const spec = method.getByLabel("Spec (JSON)");
    const body = JSON.parse(await spec.inputValue()) as { analysis_spec: { min_group_size: number } };
    body.analysis_spec.min_group_size = 50;
    await spec.fill(JSON.stringify(body, null, 2));
    await method.getByRole("button", { name: "Save and re-run" }).click();
    const notice = page.getByText(/is now version \d+\. \d+ steps? that read it re-ran; \d+ earlier verdicts? (is|are) now void\. Earlier versions stay readable/);
    await expect(notice).toBeVisible({ timeout: 120_000 });
    await expect(notice).toContainText(methodTitle);
    const rerun = thread.locator("li.step-card", { hasText: /re-ran/ }).first();
    await expect(rerun).toBeVisible();
    // the finding re-ran against the new result: its numbers still bind, so it is not flagged
    await expect(badge(rerun, "flagged")).toHaveCount(0);
    await rerun.getByRole("button", { name: /^Versions \([2-9]\d*\)/ }).click();
    const v1 = rerun.getByRole("listitem", { name: "Version 1" });
    await expect(v1.getByText(/Why void/)).toBeVisible();
    const edited = thread.locator("li.step-card", { has: page.getByRole("heading", { name: methodTitle, exact: true }) });
    await edited.getByRole("button", { name: /^Versions \([2-9]\d*\)/ }).click();
    await expect(edited.getByRole("listitem", { name: "Version 1" })).toBeVisible();
    await shot(page, "d1-thread-edited");

    // a finding (claim) has no query of its own to replay, so it is not offered a pin
    await expect(rerun.getByRole("button", { name: /^Pin/ })).toBeDisabled();
    await expect(rerun.getByText(/pin the step this one reads/)).toBeVisible();

    // pin the re-verified method step to a tile: an approval bound to the frozen query, decided by the approver
    await expect(badge(edited, "verified").first()).toBeVisible();
    await edited.getByRole("button", { name: /^Pin/ }).click();
    const pin = edited.getByRole("form", { name: `Pin ${methodTitle}` });
    await pin.getByLabel("Dashboard").fill("Live pinned findings");
    await pin.getByRole("button", { name: "Request approval to pin" }).click();
    await expect(edited.getByText(/Approval requested: an approver decides it/)).toBeVisible();
    await approveAs(browser, ws!, /Pin a verified step to a dashboard tile/);
    await edited.getByRole("button", { name: "Pin with the approved request" }).click();
    await expect(edited.getByText(/Pinned version \d+ to the tile board "Live pinned findings"/)).toBeVisible();

    // fork a "what if" branch from a step, compare it with main side by side, merge it into a report
    const forkFrom = thread.locator("li.step-card", { has: page.getByRole("heading", { name: /^H-\d+:/ }) }).nth(1);
    await forkFrom.getByRole("button", { name: /^Fork from here/ }).click();
    await page.getByLabel("Branch name").fill("what if");
    await page.getByRole("button", { name: "Fork", exact: true }).click();
    await expect(page).toHaveURL(/branch=brn_/);
    await expect(page.getByText(/Forked "what if" from/)).toBeVisible();
    await expect(page.getByRole("list", { name: "Data Thread: what if" }).getByText("from the parent branch").first()).toBeVisible();
    await page.getByLabel("Compare with").selectOption({ label: "main" });
    await page.getByRole("button", { name: "Compare side by side" }).click();
    const cmp = page.getByRole("group", { name: "Compare main with what if" });
    await expect(cmp).toBeVisible();
    await expect(cmp.getByRole("group", { name: /: shared$/ }).first()).toBeVisible();
    await shot(page, "d2-compare");
    const title = `Live thread ${Date.now().toString(36)}`;
    await page.getByLabel("Report title").fill(title);
    await page.getByRole("button", { name: "Merge what if into a report" }).click();
    const merged = page.getByText(new RegExp(`Merged into the report "${title}" \\(version 1\\) with \\d+ steps?`));
    await expect(merged).toBeVisible();
    await page.getByRole("link", { name: "Open it in Outputs" }).click();
    await expect(page).toHaveURL(/\/outputs\?type=report&artifact=art_/);
    await expect(page.getByRole("heading", { level: 2, name: new RegExp(title) }).first()).toBeVisible();
    await shot(page, "d3-merged-report");
  });
});
