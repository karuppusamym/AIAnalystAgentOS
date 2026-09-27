/**
 * (c) Restrictions, against the real API: analysts and approvers never see the gear's admin screens,
 * a direct URL to one says who it is for, and an action the server forbids explains why instead of
 * failing silently.
 */
import { ANALYST, APPROVER, expect, readState, requireLive, shot, signIn, test } from "./live";

test.describe("restrictions (P4-07, P7-18)", () => {
  requireLive();

  test("(c) analyst: no admin screens; a direct URL says who it is for", async ({ page }) => {
    const { ws } = readState();
    await signIn(page, ANALYST, `/w/${ws}`);
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav.getByRole("link", { name: "Overview" })).toBeVisible();
    for (const t of ["Capability registry", "Platform settings", "Usage & cost", "Members & policy"]) {
      await expect(nav.getByRole("link", { name: t })).toHaveCount(0);
    }
    await page.keyboard.press("Control+k");
    const palette = page.getByRole("dialog");
    await palette.getByRole("combobox").fill("platform settings");
    await expect(palette.getByRole("option", { name: /Platform settings/ })).toHaveCount(0);
    await page.keyboard.press("Escape");

    for (const [path, title] of [["/settings/platform", "Platform settings"], ["/settings/registry", "Capability registry"], ["/settings/usage", "Usage & cost"]]) {
      await page.goto(path);
      await expect(page.getByText(`${title} is for platform administrators`)).toBeVisible();
    }
    await page.goto(`/w/${ws}/settings/policy`);
    await expect(page.getByText("Workspace owners manage members and policy")).toBeVisible();
    await expect(page.getByText(/Your role here is editor/)).toBeVisible();
    await shot(page, "c1-not-entitled");
  });

  test("(c) approver: Start work explains the role it needs; a forbidden write shows the server's reason", async ({ page }) => {
    const { ws } = readState();
    await signIn(page, APPROVER, `/w/${ws}`);
    await page.getByRole("button", { name: "Start work" }).first().click();
    const kinds = page.getByRole("list", { name: "Job kinds" });
    await expect(kinds.getByRole("list", { name: "Why Explain cannot start" })).toContainText(/needs the analyst role here; you are approver/);
    await expect(kinds.getByRole("button", { name: /^Explain/ })).toHaveCount(0);
    await page.keyboard.press("Escape");

    // the brief's suggestions are an analyst's job: the approver is not offered them
    await page.goto(`/w/${ws}/data/catalog?tab=brief`);
    await expect(page.getByRole("heading", { name: /^Facts \(/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh suggestions" })).toHaveCount(0);

    // re-discovering a source is refused by the server for an approver, and the page says why
    await page.goto(`/w/${ws}/data/sources`);
    await page.getByRole("button", { name: "Discover" }).click();
    await expect(page.getByRole("alert")).toContainText(/role|not allowed|permission|forbidden/i);
    await shot(page, "c2-forbidden-action");
  });
});
