/**
 * (f) Governed ML on the real API (P5-03, P7-19): Start work → Predict opens the spec form even before a
 * spec exists; the spec is proposed from the data, saved, published and trained; the experiment shows
 * the baseline, the holdout and the model card; promotion is an approval the approver decides; the
 * champion scores approved data through a second approval.
 */
import { ANALYST, approveAs, expect, navEntries, readState, requireLive, shot, signIn, test } from "./live";

test.describe("governed ML (P5-03)", () => {
  requireLive();

  test("(f) Predict: write a spec → train and evaluate → model card → promote through an approval → score", async ({ page, browser }) => {
    const { ws } = readState();
    // the ServiceNow source's staging schema, as Data → Sources shows it
    await signIn(page, ANALYST, `/w/${ws}/data/sources`);
    const incident = await page.getByRole("checkbox", { name: "Select incident" }).locator("xpath=ancestor::tr").locator("code").innerText();
    const before = await navEntries(page);

    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Work", exact: true }).click();
    await page.getByRole("button", { name: "Start work" }).click();
    await page.getByRole("list", { name: "Job kinds" }).getByRole("button", { name: /^Predict/ }).click();
    await page.getByRole("form", { name: "Start predict" }).getByRole("button", { name: "Write a new spec" }).click();
    await expect(page).toHaveURL(`/w/${ws}/work?tab=experiments&new=predict`);

    const what = page.getByRole("form", { name: "What to predict" });
    await what.getByLabel("Table").fill(incident);
    await what.getByLabel("Target column").fill("made_sla");
    await what.getByRole("button", { name: "Propose a spec" }).click();
    const spec = page.getByRole("form", { name: "ML spec" });
    await expect(spec.getByLabel("Feature 1", { exact: true })).not.toHaveValue("");
    await expect(spec.getByRole("checkbox", { name: /dummy_prior \(baseline, always trained\)/ })).toBeDisabled();
    await shot(page, "f1-ml-spec");
    await spec.getByRole("button", { name: "Save, publish and train" }).click();

    const exp = page.getByRole("region", { name: /^Experiment mlx_/ });
    await expect(exp).toBeVisible({ timeout: 300_000 });
    await expect(exp.getByRole("table", { name: /Baseline and candidates on split/ })).toBeVisible({ timeout: 300_000 });
    await expect(exp.getByRole("note", { name: "Holdout consumed" })).toBeVisible();
    const card = exp.getByRole("region", { name: "Model card" });
    await expect(card).toContainText("Intended use");
    await shot(page, "f2-experiment-model-card");

    await exp.getByRole("button", { name: /^Request promotion of v\d+/ }).click();
    await approveAs(browser, ws!, /Promote a model version to champion/);
    await exp.getByRole("button", { name: "Continue with the approved request" }).click();
    await expect(exp.getByText(/Promoted: .* v\d+ is now the champion\./)).toBeVisible();

    await page.goto(`/w/${ws}/outputs?type=model`);
    const model = page.getByRole("region", { name: /^Model / }).first();
    await model.getByText(/^Score approved data with v\d+/).click();
    await model.getByRole("button", { name: "Prepare the scoring definition" }).click();
    await model.getByRole("button", { name: "Request approval to score" }).click();
    await approveAs(browser, ws!, /Score data with the champion model/);
    await model.getByRole("button", { name: "Continue with the approved request" }).click();
    await expect(page.getByText(/Scored [\d,]+ of [\d,]+ rows/)).toBeVisible({ timeout: 300_000 });
    await shot(page, "f3-scored");
    expect(await navEntries(page)).toBe(before);
  });
});
