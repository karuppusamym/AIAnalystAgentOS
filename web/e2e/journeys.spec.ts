import { INSIGHT, RUN, WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

test.describe("five-journey IA", () => {
  test("login → Home → Ask → Investigate → Knowledge → Operate settings", async ({ page, api }) => {
    await signIn(page);

    // Home: workspace picker, then the workspace's "What changed" landing.
    await expect(page.getByRole("heading", { name: "Workspaces", level: 1 })).toBeVisible();
    await page.getByRole("link", { name: /IT Service Management/ }).click();
    await expect(page).toHaveURL(`/w/${WS}`);
    const changed = page.locator("section", { has: page.getByRole("heading", { name: "What changed" }) });
    await expect(changed.getByText("Pending approvals")).toBeVisible();
    await expect(changed.locator(".stat", { hasText: "Open alerts" }).locator(".stat-value")).toHaveText("1");

    // Ask: a question through the side nav, answered by the governed SQL path.
    const nav = page.getByRole("navigation", { name: "Main" });
    await nav.getByRole("group", { name: "Ask" }).getByRole("link", { name: "Ask" }).click();
    await expect(page).toHaveURL(`/w/${WS}/ask`);
    await page.getByLabel("Question").fill("How many P1 incidents per assignment group?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    await expect(page.getByText("P1 incidents by assignment group.")).toBeVisible();
    await expect(page.getByText(/SELECT assignment_group/)).toBeVisible();

    // Investigate: open the run from the list.
    await nav.getByRole("link", { name: "Investigations" }).click();
    await expect(page.getByRole("heading", { name: "Investigations", level: 1 })).toBeVisible();
    await page.getByRole("link", { name: "Why are P1 resolution times rising?" }).first().click();
    await expect(page).toHaveURL(`/w/${WS}/investigate/${RUN}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Why are P1 resolution times rising?");

    // Knowledge: the increment-3 catalog now lives here.
    await nav.getByRole("group", { name: "Knowledge" }).getByRole("link", { name: "Catalog" }).click();
    await expect(page).toHaveURL(`/w/${WS}/knowledge/catalog`);
    await expect(page.getByRole("heading", { name: "Catalog", level: 1 })).toBeVisible();
    await expect(page.getByText("Incidents").first()).toBeVisible();

    // Operate: platform settings (admin), reached through the command palette.
    await page.keyboard.press("Control+k");
    const palette = page.getByRole("dialog");
    await expect(palette).toBeVisible();
    await palette.getByRole("combobox").fill("platform settings");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL("/operate/settings");
    await expect(page.getByRole("heading", { name: "Platform settings", level: 1 })).toBeVisible();
    await expect(palette).toBeHidden();

    expect(api.unmatched).toEqual([]);
  });

  test("old URLs redirect into the new journeys", async ({ page }) => {
    await signIn(page, `/w/${WS}/monitoring?tab=alerts`);
    await expect(page).toHaveURL(`/w/${WS}/operate/monitoring?tab=alerts`);
    await page.goto("/admin");
    await expect(page).toHaveURL("/operate/registry");
    await page.goto(`/w/${WS}/insights/${INSIGHT}`);
    await expect(page).toHaveURL(`/w/${WS}/investigate/findings/${INSIGHT}`);
  });

  test("theme toggle persists across reloads", async ({ page }) => {
    await signIn(page);
    await expect(page.getByRole("heading", { name: "Workspaces", level: 1 })).toBeVisible();
    const html = page.locator("html");
    await page.getByRole("button", { name: /System theme/ }).click();
    await expect(html).toHaveAttribute("data-theme", "light");
    await page.getByRole("button", { name: /Light theme/ }).click();
    await expect(html).toHaveAttribute("data-theme", "dark");
    await page.reload();
    await expect(html).toHaveAttribute("data-theme", "dark");
  });
});

/** Main screen of each journey: [journey, path, text that shows the data has loaded]. */
const SCREENS: [string, string, RegExp][] = [
  ["Home", `/w/${WS}`, /What changed/],
  ["Ask", `/w/${WS}/ask`, /SQL console/],
  ["Investigate", `/w/${WS}/investigate/${RUN}`, /Why are P1 resolution times rising/],
  ["Knowledge", `/w/${WS}/knowledge/catalog`, /One row per incident/],
  ["Build", `/w/${WS}/build/studio`, /P1 resolution/],
  ["Operate", `/w/${WS}/operate/approvals`, /Publish dashboards/],
  ["Operate · settings", "/operate/settings", /Purpose/],
  ["Operate · usage", "/operate/usage", /Tokens saved/],
];

for (const scheme of ["light", "dark"] as const) {
  test.describe(`accessibility (${scheme})`, () => {
    test.use({ colorScheme: scheme });
    for (const [journey, path, ready] of SCREENS) {
      test(`${journey} has no axe violations`, async ({ page }) => {
        await signIn(page, path);
        await expect(page.locator("main")).toContainText(ready);
        expect(await axeViolations(page)).toEqual([]);
      });
    }
    test("command palette has no axe violations", async ({ page }) => {
      await signIn(page, `/w/${WS}`);
      await expect(page.locator("main")).toContainText(/What changed/);
      await page.keyboard.press("Control+k");
      await expect(page.getByRole("dialog")).toBeVisible();
      expect(await axeViolations(page)).toEqual([]);
    });
  });
}
