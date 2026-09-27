/**
 * Governed ML UI (P5-03, workbench-ux §5 ML journey): Predict opens the MLSpec form prefilled from the
 * proposals endpoint; readiness/leakage; the split diagram; the baseline/candidate table with split,
 * version and metric direction together (never ranked across holdouts); holdout results marked
 * consumed; the model card; promotion, scoring and rollback through approvals; model monitors in Operate.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { session } from "../api";
import { draftFromSpec, draftProblems, specFromDraft } from "../components/Ml";
import { mlMonitorConfig, mlMonitorProblems } from "../components/OperateHealth";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { decide } from "./mockApprovals";
import { mockBackend, resetMockState, USER, WS } from "./mockBackend";
import { proposal } from "./mockMl";

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

async function proposeAndFill(target = "breached_sla") {
  const what = await screen.findByRole("form", { name: "What to predict" });
  fireEvent.change(within(what).getByLabelText("Table"), { target: { value: "stg_sn.incident" } });
  fireEvent.change(within(what).getByLabelText("Target column"), { target: { value: target } });
  fireEvent.click(within(what).getByRole("button", { name: "Propose a spec" }));
  return screen.findByRole("form", { name: "ML spec" });
}

describe("the MLSpec form", () => {
  it("Start work → Predict opens the form, prefilled from the rules-first proposal", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work`);
    fireEvent.click(await screen.findByRole("button", { name: "Start work" }));
    fireEvent.click(within(await screen.findByRole("list", { name: "Job kinds" })).getByRole("button", { name: /Predict/ }));
    fireEvent.click(within(await screen.findByRole("form", { name: "Start predict" })).getByRole("button", { name: "Write a new spec" }));
    expect(await screen.findByRole("tab", { name: "Experiments", selected: true })).toBeTruthy();
    const form = await proposeAndFill();
    expect(bodyOf(calls(f, "POST", /\/ml\/proposals$/)[0][1])).toMatchObject({ asset: "stg_sn.incident", target: "breached_sla" });
    expect(screen.getByText(/Proposed by the rules \(no model call\)/)).toBeTruthy();
    expect((within(form).getByLabelText("Task") as HTMLSelectElement).value).toBe("classify");
    expect((within(form).getByLabelText("Feature 1") as HTMLInputElement).value).toBe("priority");
    expect((within(form).getByLabelText("Feature 1 is known") as HTMLSelectElement).value).toBe("cutoff");
    const baseline = within(form).getByRole("checkbox", { name: /dummy_prior \(baseline, always trained\)/ }) as HTMLInputElement;
    expect(baseline.checked && baseline.disabled).toBe(true);
    expect(within(form).getByRole("option", { name: "roc_auc (higher is better)" })).toBeTruthy();
    expect(within(form).getByRole("option", { name: "log_loss (lower is better)" })).toBeTruthy();
  });

  it("a feature declared known only after the outcome is refused before anything is sent", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work?tab=experiments&new=predict`);
    const form = await proposeAndFill();
    fireEvent.change(within(form).getByLabelText("Feature 3 is known"), { target: { value: "after_outcome" } });
    expect(within(form).getByRole("list", { name: "Problems with the spec" }).textContent).toMatch(/leakage/);
    expect((within(form).getByRole("button", { name: "Save, publish and train" }) as HTMLButtonElement).disabled).toBe(true);
    expect(calls(f, "POST", /\/definitions$/)).toEqual([]);
  });

  it("a leak only the server can see (timestamps after the cutoff) opens the refused experiment's readiness", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work?tab=experiments&new=predict`);
    const form = await proposeAndFill();
    fireEvent.click(within(form).getByRole("button", { name: "Add a feature" }));
    fireEvent.change(within(form).getByLabelText("Feature 4"), { target: { value: "resolved_at" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save, publish and train" }));
    const checks = await screen.findByRole("region", { name: "Readiness and leakage checks" });
    expect(within(checks).getByText(/Refused before training: 1 check failed\. Nothing was trained and no holdout was read\./)).toBeTruthy();
    const row = within(checks).getByText("post cutoff timestamps").closest("tr")!;
    expect(row.textContent).toMatch(/fail/);
    expect(row.textContent).toMatch(/resolved_at \(812 rows\)/);
    expect(screen.queryByRole("region", { name: "Holdout results" })).toBeNull();
  });

  it("maps drafts to specs: the baseline is always first and a random split needs a reason", () => {
    const d = draftFromSpec(proposal({ asset: "stg_sn.incident" }).proposal!, "p1_breach");
    const spec = specFromDraft({ ...d, estimators: ["gradient_boosting"] });
    expect(spec.estimators).toEqual(["dummy_prior", "gradient_boosting"]);
    expect(spec.positive_class).toBe(true);
    expect(draftProblems({ ...d, strategy: "random", justification: "" })).toContain(
      "A random split needs a reason the rows are independent; otherwise use a chronological or group split.");
    expect(draftProblems({ ...d, key: "Bad Name" })[0]).toMatch(/lowercase/);
  });
});

describe("an experiment: split, trials, holdout, card, promotion", () => {
  it("save → publish → train, then shows split, trials with split and metric direction, a consumed holdout and the card", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work?tab=experiments&new=predict`);
    const form = await proposeAndFill();
    fireEvent.click(within(form).getByRole("button", { name: "Save, publish and train" }));
    const exp = await screen.findByRole("region", { name: "Experiment mlx_1" });
    expect(bodyOf(calls(f, "POST", /\/definitions$/)[0][1])).toMatchObject({ kind: "ml_spec", key: "incident_breached_sla" });
    expect(new Headers(calls(f, "POST", /\/definitions\/defn_ml1\/publish$/)[0][1]!.headers).get("If-Match")).toBe('"1"');
    expect(bodyOf(calls(f, "POST", /\/ml\/experiments$/)[0][1])).toEqual({ definition: "defn_ml1" });

    expect(within(exp).getByText("beats its baseline")).toBeTruthy();
    const diagram = await within(exp).findByRole("img", { name: /chronological split \(seed 7\): 3,150 training rows, an embargo of 1 period and 800 holdout rows/ });
    expect(diagram).toBeTruthy();
    expect(within(exp).getByRole("list", { name: "Validation folds" }).children).toHaveLength(3);

    const table = await within(exp).findByRole("table", { name: /Baseline and candidates on split 5p1173a9f0e3/ });
    expect(table.querySelector("caption")!.textContent).toMatch(/roc_auc, higher is better · validation folds only/);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => (r as HTMLTableRowElement).cells[0].textContent)).toEqual(["0", "1", "2", "3"]); // trial order, not a ranking
    expect(within(rows[0]).getByText("baseline")).toBeTruthy();
    expect(within(rows[2]).getByText("selected")).toBeTruthy();
    expect(within(rows[3]).getByText("time cap reached")).toBeTruthy();

    const holdout = within(exp).getByRole("region", { name: "Holdout results" });
    expect(holdout.textContent).toMatch(/Gain over the baseline0\.274 \(95% interval 0\.221 to 0\.318\)/);
    expect(within(holdout).getByRole("note", { name: "Holdout consumed" }).textContent).toMatch(/read once, after the selection/);

    const other = within(exp).getByText(/Earlier experiments of p1_breach/).closest("details")!;
    expect(other.textContent).toMatch(/different holdout: not ranked/);
    expect(within(exp).getByRole("region", { name: "Model card" }).textContent).toMatch(/bound to a recorded fact/);
  });

  it("promotion needs an approval decided in the inbox", async () => {
    const f = mockFetch();
    mockBackend("POST", `/api/workspaces/${WS}/definitions`, JSON.stringify({ kind: "ml_spec", key: "p1_breach", spec: proposal({ asset: "stg_sn.incident" }).proposal }));
    mockBackend("POST", `/api/workspaces/${WS}/definitions/defn_ml1/publish`, "{}");
    mockBackend("POST", `/api/workspaces/${WS}/ml/experiments`, JSON.stringify({ definition: "defn_ml1" }));
    renderAt(`/w/${WS}/work?tab=experiments&experiment=mlx_1`);
    const promote = await screen.findByRole("region", { name: "Promote" });
    expect(within(promote).getByText("challenger")).toBeTruthy();
    fireEvent.click(within(promote).getByRole("button", { name: "Request promotion of v2" }));
    const pending = await within(promote).findByRole("status", { name: "Approval for promoting p1_breach v2" });
    expect(within(pending).getByRole("link", { name: "approvals inbox" })).toBeTruthy();
    fireEvent.click(within(pending).getByRole("button", { name: "Continue with the approved request" }));
    expect(await within(promote).findByText(/not approved yet/)).toBeTruthy();
    decide("apr_promote", "approve");
    fireEvent.click(within(pending).getByRole("button", { name: "Continue with the approved request" }));
    expect(await within(promote).findByText("Promoted: p1_breach v2 is now the champion.")).toBeTruthy();
    expect(calls(f, "POST", /\/ml\/models\/mlv_1b\/promote$/).map(([, i]) => bodyOf(i))).toEqual([{ approval_id: null }, { approval_id: "apr_promote" }, { approval_id: "apr_promote" }]);
  });
});

describe("Outputs → Models & scoring", () => {
  it("shows champion and challenger without ranking across holdouts, and scores approved data with rejected rows visible", async () => {
    mockFetch();
    mockBackend("POST", `/api/workspaces/${WS}/definitions`, JSON.stringify({ kind: "ml_spec", key: "p1_breach", spec: proposal({ asset: "stg_sn.incident" }).proposal }));
    mockBackend("POST", `/api/workspaces/${WS}/definitions/defn_ml1/publish`, "{}");
    mockBackend("POST", `/api/workspaces/${WS}/ml/experiments`, JSON.stringify({ definition: "defn_ml1" }));
    renderAt(`/w/${WS}/outputs?type=model`);
    const model = await screen.findByRole("region", { name: "Model p1_breach" });
    expect(within(model).getByRole("table", { name: /roc_auc \(higher is better\) on each version's own holdout/ })).toBeTruthy();
    expect(within(model).getByText(/evaluated on different holdouts, so their scores are not\s+ranked/)).toBeTruthy();
    fireEvent.click(within(model).getByText("Score approved data with v1"));
    const form = within(model).getByRole("form", { name: "Score with p1_breach v1" });
    fireEvent.click(within(form).getByRole("button", { name: "Prepare the scoring definition" }));
    fireEvent.click(await within(model).findByRole("button", { name: "Request approval to score" }));
    const pending = await within(model).findByRole("status", { name: /Approval for scoring stg_sn.incident/ });
    decide("apr_score_1", "approve");
    fireEvent.click(within(pending).getByRole("button", { name: "Continue with the approved request" }));
    expect(await screen.findByText(/Scored 4,198 of 4,210 rows into aos_out\.ml_p1_breach_scores; 12 rejected rows are kept in aos_out\.ml_p1_breach_scores_rejected\./)).toBeTruthy();
    const runs = await screen.findByRole("table", { name: "Scoring runs" });
    await waitFor(() => expect(runs.textContent).toMatch(/succeeded/));
    expect(within(runs).getByText("12").tagName).toBe("STRONG");
  });

  it("rolls back to the previous champion through an approval", async () => {
    mockFetch();
    for (const [p, b] of [[`/definitions`, { kind: "ml_spec", key: "p1_breach", spec: {} }], [`/definitions/defn_ml1/publish`, {}], [`/ml/experiments`, { definition: "defn_ml1" }],
      [`/ml/models/mlv_1b/promote`, {}]] as const) mockBackend("POST", `/api/workspaces/${WS}${p}`, JSON.stringify(b));
    decide("apr_promote", "approve");
    mockBackend("POST", `/api/workspaces/${WS}/ml/models/mlv_1b/promote`, JSON.stringify({ approval_id: "apr_promote" }));
    renderAt(`/w/${WS}/outputs?type=model`);
    const model = await screen.findByRole("region", { name: "Model p1_breach" });
    fireEvent.click(await within(model).findByRole("button", { name: "Roll back to the previous champion" }));
    await within(model).findByRole("status", { name: /Approval for rolling p1_breach back from v2/ });
    decide("apr_rollback", "approve");
    fireEvent.click(within(model).getByRole("button", { name: "Continue with the approved request" }));
    expect(await screen.findByText(/Rolled back: p1_breach v1 is the champion again; v2 is retired\./)).toBeTruthy();
  });
});

describe("Operate → Models & pipelines: model monitors", () => {
  it("shows drift (never claiming a performance loss) and creates a label-aware performance monitor", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/operate/monitoring?tab=health`);
    const list = await screen.findByRole("list", { name: "Model monitors" });
    expect(list.textContent).toMatch(/drift alone does not show a loss of performance/);
    expect(within(list).getByText("alerting")).toBeTruthy();
    fireEvent.click(await screen.findByText("Watch a model"));
    const form = screen.getByRole("form", { name: "Watch a model" });
    fireEvent.change(within(form).getByLabelText("Watch for"), { target: { value: "ml_performance" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create model monitor" }));
    expect(within(form).getByRole("alert").textContent).toMatch(/Name the monitor.*Name the table and column/);
    fireEvent.change(within(form).getByLabelText("Monitor name"), { target: { value: "p1_breach AUC" } });
    fireEvent.change(within(form).getByLabelText("Label table"), { target: { value: "stg_sn.incident" } });
    fireEvent.change(within(form).getByLabelText("Label column"), { target: { value: "breached_sla" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create model monitor" }));
    expect(await screen.findByText(/Model monitor created/)).toBeTruthy();
    expect(bodyOf(calls(f, "POST", /\/monitors$/)[0][1])).toEqual({ name: "p1_breach AUC", kind: "ml_performance", auto_investigate: false,
      config: { model: "p1_breach", label_asset: "stg_sn.incident", label_column: "breached_sla", label_horizon_days: 14, tolerance: 0.05, min_labels: 30 } });
    expect(await within(screen.getByRole("list", { name: "Model monitors" })).findByText("p1_breach AUC")).toBeTruthy();
  });

  it("validates each model monitor kind's fields", () => {
    expect(mlMonitorProblems("ml_drift", { name: "d", model: "m", psi: "0" })).toEqual(["The PSI threshold must be positive."]);
    expect(mlMonitorProblems("ml_freshness", { name: "f", model: "m", maxAge: "" })).toEqual(["The maximum age must be a positive number of hours."]);
    expect(mlMonitorConfig("ml_drift", { model: "m", psi: "0.3" })).toEqual({ model: "m", psi_threshold: 0.3 });
  });
});
