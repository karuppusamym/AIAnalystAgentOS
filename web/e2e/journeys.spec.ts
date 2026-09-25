import { INSIGHT, RUN, THREAD_NEW, WS } from "../src/test/mockBackend";
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

test.describe("Ask (P4-U02)", () => {
  test("ask → promote to monitor → investigate", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/ask`);
    await page.getByLabel("Question").fill("How many P1 incidents per assignment group?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    const answer = page.getByRole("article", { name: "Question 1" });
    await expect(answer.getByText("P1 incidents by assignment group.")).toBeVisible();
    await expect(answer.getByRole("list", { name: "Provenance" }).getByText("Validated by the query gateway")).toBeVisible();
    await expect(answer.getByRole("img", { name: /bar chart/ })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`thread=${THREAD_NEW}`));

    const inspector = page.getByRole("complementary", { name: "Answer inspector" });
    await inspector.getByRole("tab", { name: "Decision" }).click();
    await expect(inspector.getByText(/ask_route: generate — decided by rule/)).toBeVisible();

    const promote = answer.getByRole("region", { name: "Promote this answer" });
    await promote.getByRole("button", { name: "Monitor this" }).click();
    await promote.getByLabel("Every").selectOption("month");
    await promote.getByRole("button", { name: "Create monitor" }).click();
    await expect(promote.getByText(/Monitor "P1 incidents per assignment group" created/)).toBeVisible();

    await promote.getByRole("button", { name: "Investigate why" }).click();
    await expect(page).toHaveURL(`/w/${WS}/investigate/${RUN}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Why are P1 resolution times rising?");
    expect(api.unmatched).toEqual([]);
  });

  test("a vague question gets the clarify refusal with its remedy", async ({ page }) => {
    await signIn(page, `/w/${WS}/ask`);
    await page.getByLabel("Question").fill("what about it?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    const turn = page.getByRole("article", { name: "Question 1" });
    await expect(turn.getByText("The question needs more detail")).toBeVisible();
    await expect(turn.getByRole("button", { name: "Rephrase the question" })).toBeVisible();
  });
});

test.describe("investigation board (P4-U03)", () => {
  test("board → why trust this → reject a finding → redirect by chat", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/investigate/${RUN}`);
    const supported = page.getByRole("listitem", { name: /Supported/ });
    await expect(supported.getByText("Network resolves P1s slower")).toBeVisible();
    // Raw JSON is never on the default path: every .json block sits in a closed Technical details.
    const stray = await page.locator(".json").evaluateAll((els) => els.filter((e) => !e.closest("details[data-technical]:not([open])")).length);
    expect(stray).toBe(0);

    const f1 = page.getByRole("article", { name: "Finding F1" });
    await f1.getByRole("button", { name: "Why trust this" }).click();
    const dialog = page.getByRole("dialog", { name: "Why trust F1?" });
    await expect(dialog.getByText(/identical result hash on re-run/)).toBeVisible();
    await expect(dialog.getByText(/q = 0\.0010/)).toBeVisible();
    await expect(dialog.getByText(/representative \(time_window\)/)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(f1.getByRole("button", { name: "Why trust this" })).toBeFocused();

    await f1.getByRole("button", { name: "Reject" }).click();
    await f1.getByLabel(/Why is F1 wrong/).fill("Network was reorganised in August.");
    await f1.getByRole("button", { name: "Reject finding" }).click();
    await expect(f1.getByText(/Replanned to plan v2/)).toBeVisible();

    await page.getByLabel("Message").fill("Focus on the Network group");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText(/Focus on the Network group\./)).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });
});

test.describe("operate (P4-U06, P4-U07)", () => {
  test("registry lists a newly installed plugin and runs it from its generated form", async ({ page, api }) => {
    await signIn(page, "/operate/registry");
    const methods = page.getByRole("table", { name: "Analysis methods" });
    await expect(methods.getByText("method.acme_funnel")).toBeVisible();
    await expect(methods.getByText("entrypoint:acme-methods")).toBeVisible();
    await page.getByLabel("Workspace for enablement").selectOption(WS);
    await expect(page).toHaveURL(new RegExp(`ws=${WS}`));
    const toggle = page.getByRole("switch", { name: "Enable method.acme_funnel in this workspace" });
    await expect(toggle).not.toBeChecked();
    await page.locator("label.switch", { has: toggle }).click();
    await expect(toggle).toBeChecked();

    await page.getByRole("button", { name: "Open method.acme_funnel" }).click();
    const run = page.getByRole("region", { name: "Run this capability" });
    await run.getByRole("button", { name: "Run" }).click();
    await expect(run.getByText("This field is required.")).toBeVisible();
    await run.getByLabel(/^Asset/).fill("events");
    await run.getByRole("button", { name: "Add steps" }).click();
    await run.getByRole("button", { name: "Add steps" }).click();
    await run.getByRole("textbox", { name: "Steps 1" }).fill("visit");
    await run.getByRole("textbox", { name: "Steps 2" }).fill("buy");
    await run.getByRole("button", { name: "Run" }).click();
    const result = page.locator("[data-renderer='renderer.stat_result']");
    await expect(result.getByText("0.0042", { exact: true })).toBeVisible();
    await expect(result.getByText("cramers_v", { exact: true })).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });

  test("approval inbox shows the payload diff, hashes and policy version", async ({ page }) => {
    await signIn(page, `/w/${WS}/operate/approvals`);
    const diff = page.getByRole("table", { name: "Payload changes" });
    await expect(diff.getByText("dashboards[key=p1_resolution].title")).toBeVisible();
    await expect(diff.getByText("P1 resolution (weekly)")).toBeVisible();
    await expect(page.getByText("(current v2)")).toBeVisible();
  });

  test("alerts explain their triage (rule, JEV probability, escalate-only)", async ({ page }) => {
    await signIn(page, `/w/${WS}/operate/monitoring?tab=alerts`);
    const triage = page.getByLabel("Triage explanation");
    await expect(triage.getByText(/Rule: MTTR 9\.4h > 8h/)).toBeVisible();
    await expect(triage.getByText(/JEV can only raise severity/)).toBeVisible();
  });
});

/** Main screen of each journey: [journey, path, text that shows the data has loaded]. */
const SCREENS: [string, string, RegExp][] = [
  ["Home", `/w/${WS}`, /What changed/],
  ["Ask", `/w/${WS}/ask`, /SQL console/],
  ["Ask · thread", `/w/${WS}/ask?thread=ask_old`, /How many P1 incidents per week/],
  ["Investigate", `/w/${WS}/investigate/${RUN}`, /Why are P1 resolution times rising/],
  ["Knowledge", `/w/${WS}/knowledge/catalog`, /One row per incident/],
  ["Build", `/w/${WS}/build/studio`, /P1 resolution/],
  ["Operate", `/w/${WS}/operate/approvals`, /Publish dashboards/],
  ["Operate · settings", "/operate/settings", /Purpose/],
  ["Operate · usage", "/operate/usage", /Spend by rung not reported/],
  ["Operate · registry", `/operate/registry?ws=${WS}&cap=method.acme_funnel`, /acme_methods\/tests\/test_funnel\.py/],
  ["Operate · alerts", `/w/${WS}/operate/monitoring?tab=alerts`, /JEV can only raise severity/],
  ["Operate · policy", `/w/${WS}/operate/governance`, /Effective policy/],
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
    test("why-trust drawer has no axe violations", async ({ page }) => {
      await signIn(page, `/w/${WS}/investigate/${RUN}`);
      await page.getByRole("article", { name: "Finding F1" }).getByRole("button", { name: "Why trust this" }).click();
      await expect(page.getByRole("dialog")).toContainText(/identical result hash/);
      expect(await axeViolations(page)).toEqual([]);
    });
    test("command palette has no axe violations", async ({ page }) => {
      await signIn(page, `/w/${WS}`);
      await expect(page.locator("main")).toContainText(/What changed/);
      await page.keyboard.press("Control+k");
      await expect(page.getByRole("dialog")).toBeVisible();
      expect(await axeViolations(page)).toEqual([]);
    });
  });
}
