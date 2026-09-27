/**
 * (a) Analyst and (b) approver, through the real UI and API: an owner creates the workspace and its
 * members; the analyst connects the ServiceNow mock, discovers, selects tables, confirms the brief,
 * starts an Explain investigation and watches it reach verified findings; the approver approves the
 * publication in their own session and the analyst sees the investigation complete; "Why this number?"
 * shows every link; a report is generated.
 */
import { ADMIN, ANALYST, APPROVER, approveAs, badge, expect, navEntries, readState, requireLive, shot, signIn, test, writeState } from "./live";

const OBJECTIVE = "Why are P1 incident resolution times increasing, and which assignment groups drive SLA breaches?";

test.describe.serial("analyst and approver (P4-07)", () => {
  requireLive();

  test("owner: create the workspace with every work mode, add the analyst and the approver", async ({ page }) => {
    const stamp = `${Date.now().toString(36)}`;
    const name = `Live journeys ${stamp}`;
    await signIn(page, ADMIN, "/");
    await expect(page.getByRole("heading", { name: "Workspaces", level: 1 })).toBeVisible();
    await page.getByRole("button", { name: "New workspace" }).click();
    await page.getByLabel("Name").fill(name);
    await page.getByLabel("Business objective").fill(OBJECTIVE);
    await page.getByRole("checkbox", { name: /Data engineering/ }).check();
    await page.getByRole("checkbox", { name: /ML and experiments/ }).check();
    await page.getByRole("button", { name: "Create workspace" }).click();
    await expect(page).toHaveURL(/\/w\/ws_[a-z0-9]+$/);
    const ws = new URL(page.url()).pathname.split("/")[2];
    writeState({ ws, wsName: name, stamp, run: undefined });
    await expect(page.getByRole("list", { name: "Getting started" })).toContainText("Connect or upload data");

    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Members & policy" }).click();
    const addMember = page.getByRole("form", { name: "Add a member" });
    for (const [email, role] of [[ANALYST, "editor"], [APPROVER, "approver"]]) {
      await addMember.getByLabel("Email", { exact: true }).fill(email);
      await addMember.getByLabel("Role", { exact: true }).selectOption(role);
      await addMember.getByRole("button", { name: "Add member" }).click();
      await expect(page.getByText(email)).toBeVisible();
    }
    await expect(page.getByRole("heading", { name: "Members (3)" })).toBeVisible();

    // v1 §62 predates the approved-metric gate (P4-K03): KPIs proposed by a run are approved separately,
    // so this workspace publishes without it, as scripts/e2e_demo.py does. Set through the policy's Advanced JSON.
    const form = page.getByRole("form", { name: "Workspace policy" });
    await form.getByText("Advanced: the whole policy as JSON").click();
    const json = form.getByLabel("Policy document (JSON)");
    const doc = JSON.parse(await json.inputValue()) as Record<string, unknown>;
    await json.fill(JSON.stringify({ ...doc, require_approved_metrics: false }, null, 2));
    await form.getByRole("button", { name: "Save policy" }).click();
    await expect(page.getByRole("heading", { name: /Effective policy · version 2/ })).toBeVisible();
  });

  test("(a) analyst: connect ServiceNow, discover, select, confirm the brief, Start work → Explain → verified findings", async ({ page }) => {
    const { ws } = readState();
    await signIn(page, ANALYST, `/w/${ws}`);
    const nav = page.getByRole("navigation", { name: "Main" });
    for (const t of ["Capability registry", "Platform settings", "Usage & cost", "Members & policy"]) {
      await expect(nav.getByRole("link", { name: t })).toHaveCount(0);
    }
    const before = await navEntries(page);

    // connect the ServiceNow mock (the API's configured instance URL), discover, select two tables
    await page.getByRole("link", { name: "Connect data" }).click();
    await expect(page).toHaveURL(`/w/${ws}/data/sources`);
    await page.getByRole("button", { name: "Add source" }).click();
    const add = page.getByRole("form", { name: "Add source" });
    await expect(add.getByLabel("Kind", { exact: true })).toHaveValue("servicenow");
    await add.getByRole("textbox", { name: "Name *", exact: true }).fill("ServiceNow");
    await add.getByLabel("Username *").fill("admin");
    await add.getByText("Optional settings").click();
    await add.getByLabel("Tables").fill("incident, change_request, sys_user_group, cmdb_ci");
    await add.getByRole("button", { name: "Add source" }).click();
    await page.getByRole("button", { name: "Discover" }).click();
    await expect(page.getByRole("status").filter({ hasText: /Discovered 4 assets/ })).toBeVisible({ timeout: 60_000 });
    await page.getByRole("checkbox", { name: "Select incident" }).check();
    await page.getByRole("checkbox", { name: "Select change_request" }).check();
    await page.getByRole("button", { name: "Save selection" }).click();
    await expect(page.getByText("Selected 2 assets; loaded 2 into staging.")).toBeVisible({ timeout: 120_000 });
    await shot(page, "a1-sources-selected");

    // the brief: draft suggestions from the catalog, then confirm each open question
    await nav.getByRole("link", { name: "Catalog & definitions" }).click();
    await page.getByRole("tab", { name: "Brief & readiness" }).click();
    await page.getByRole("button", { name: "Refresh suggestions" }).click();
    await expect(page.getByText(/Suggestions refreshed: \d+ added/)).toBeVisible();
    const open = page.getByRole("region", { name: /^Open questions/ });
    for (let i = 0; i < 20 && await open.getByRole("button", { name: /^Confirm / }).count(); i++) {
      const count = await open.getByRole("button", { name: /^Confirm / }).count();
      await open.getByRole("button", { name: /^Confirm / }).first().click();
      await expect(open.getByRole("button", { name: /^Confirm / })).toHaveCount(count - 1);
    }
    await expect(page.getByRole("heading", { name: "Open questions (0)" })).toBeVisible();
    await expect(page.getByRole("region", { name: /^Facts \([1-9]/ })).toBeVisible();

    // Start work → Explain: the brief is prefilled from the objective, the preflight shows what it reads
    await nav.getByRole("link", { name: "Overview" }).click();
    await page.getByRole("button", { name: "Start work" }).first().click();
    const dialog = page.getByRole("dialog", { name: "Start work" });
    await dialog.getByRole("button", { name: /^Explain/ }).click();
    const form = page.getByRole("form", { name: "Start explain" });
    await expect(form.getByLabel("What should the investigation find out?")).toHaveValue(OBJECTIVE);
    await expect(form.getByRole("region", { name: "Before it starts" })).toContainText("ServiceNow");
    await expect(form.getByRole("group", { name: "Readiness for explain" })).toBeVisible();
    await shot(page, "a2-start-work-explain");
    await form.getByRole("button", { name: "Start investigation" }).click();
    await expect(page).toHaveURL(/\/work\/investigations\/run_[a-z0-9]+$/);
    const run = new URL(page.url()).pathname.split("/").pop()!;
    writeState({ run });

    // the live board: hypotheses tested, findings verified, then it waits for the publication approval
    await expect(page.getByText("1 approval waiting — see the Approvals panel.")).toBeVisible({ timeout: 300_000 });
    await expect(badge(page.locator("main"), "WAITING_USER").first()).toBeVisible();
    const verified = page.locator("article[aria-label^='Finding ']", { hasText: "verified" });
    expect(await verified.count()).toBeGreaterThanOrEqual(3);
    await expect(page.getByRole("heading", { name: "Approvals (1 pending)" })).toBeVisible();
    await shot(page, "a3-investigation-waiting");

    expect(await navEntries(page)).toBe(before);
  });

  test("(b) approver approves the publication in their own session; the analyst sees it complete", async ({ page, browser }) => {
    const { ws, run } = readState();
    await signIn(page, ANALYST, `/w/${ws}/work/investigations/${run}`);
    await expect(badge(page.locator("main"), "WAITING_USER").first()).toBeVisible();

    // the analyst may not approve their own publication: the inbox shows why when they try
    await page.goto(`/w/${ws}/operate/approvals`);
    const list = page.getByRole("complementary", { name: "Approval proposals" });
    await list.getByRole("button", { name: /Publish dashboards/ }).click();
    const card = page.locator("article.approval-pending", { hasText: "Publish dashboards" });
    await card.getByRole("button", { name: "Approve" }).click();
    await expect(card.getByRole("alert")).toContainText(/approver|role|not allowed|permission/i);

    await approveAs(browser, ws!, /Publish dashboards/);

    await page.goto(`/w/${ws}/work/investigations/${run}`);
    await expect(badge(page.locator("main"), "COMPLETED").first()).toBeVisible({ timeout: 180_000 });
    const approvals = page.getByRole("complementary").locator("article", { hasText: "Publish dashboards" });
    await expect(badge(approvals, "executed")).toBeVisible();
    await expect(approvals).toContainText("reviewed in the live journey");
    await shot(page, "b1-investigation-completed");
  });

  test("(a) Why this number? shows every link, and a report is generated", async ({ page }) => {
    const { ws, run } = readState();
    await signIn(page, ANALYST, `/w/${ws}/work/investigations/${run}`);
    const finding = page.locator("article[aria-label^='Finding ']", { hasText: "verified" }).first();
    await finding.getByRole("button", { name: "Why this number?" }).click();
    const drawer = page.getByRole("dialog", { name: "Why this number?" });
    await expect(drawer.getByText("Verification: verified")).toBeVisible();
    await expect(drawer.getByRole("status")).toHaveText("Every link behind these numbers still holds.");
    const number = drawer.getByRole("region", { name: /^Number / }).first();
    for (const link of ["The fact behind it", "The step that produced it", "The query that read the data", "The data it was read from",
      "The metric definition", "The verification"]) {
      await expect(number.getByText(link)).toBeVisible();
    }
    await number.getByText("Show the SQL").first().click();
    await expect(number.getByText(/SELECT/i).first()).toBeVisible();
    await shot(page, "a4-why-this-number");
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();

    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Outputs" }).click();
    await page.getByRole("navigation", { name: "Filter outputs by type" }).getByRole("link", { name: /^Reports/ }).click();
    await page.getByText("Generate a report").click();
    const gen = page.getByRole("form", { name: "Generate report" });
    await gen.getByLabel("Kind").selectOption({ label: "Executive" });
    await gen.getByRole("button", { name: "Generate report" }).click();
    const report = page.getByRole("region", { name: /^Report .* executive report$/ });
    await expect(report).toBeVisible({ timeout: 60_000 });
    await expect(report).toContainText(/\d+ findings/);
    await expect(report.getByRole("button", { name: "Download PDF" })).toBeVisible();
    await expect(report.getByRole("link", { name: "Source investigation" })).toHaveAttribute("href", `/w/${ws}/work/investigations/${run}`);
    await shot(page, "a5-report");
  });
});
