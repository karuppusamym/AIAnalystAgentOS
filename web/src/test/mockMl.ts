/**
 * Governed ML in memory (docs/30-runbooks/ml-api.md): rules-first proposals, ml_spec / ml_scoring
 * definitions (draft → publish), experiments refused for leakage or trained against a baseline on one
 * sealed holdout, model versions (champion, challenger), promotion and rollback through approvals,
 * approved batch scoring with duplicates detected, and ML monitors.
 */
import type { DefinitionVersion, MLExperiment, MLProposal, ModelVersion, Monitor, ScoringRun } from "../api";
import { approvalStatus, requestApproval } from "./mockApprovals";

const T = "2026-09-26T12:00:00Z";
export const MODEL = "p1_breach";
export const PKG_OLD = "a".repeat(64);
export const PKG_NEW = "b".repeat(64);
export const MANIFEST_OLD = "0ld5p1173a9f0e3c";
export const MANIFEST_NEW = "5p1173a9f0e3c4d2";

type Reply = { status: number; body: unknown } | null;
const ok = (body: unknown, status = 200): Reply => ({ status, body });
const err = (status: number, code: string, message: string, details: Record<string, unknown> = {}): Reply =>
  ({ status, body: { error: { code, message, retryable: false, details } } });

const verified = (id: string) => ({ state: "ACTIVE", badge: "verified", record_id: `ver_${id}`, verdict: "verified", verifier: "ml",
  fingerprint: `fp_${id}`, dependencies: [], created_at: T, void: null, flags: [] });

function version(id: string, v: number, exp: string, status: string, pkg: string, auc: number, over: Partial<ModelVersion> = {}): ModelVersion {
  return { id, workspace_id: "ws_demo", name: MODEL, version: v, experiment_id: exp, task: "classify", package_hash: pkg, status,
    feature_schema: [{ column: "priority" }, { column: "assignment_group" }, { column: "reassignment_count" }],
    metrics: { metric: "roc_auc", candidate: { roc_auc: auc }, baseline: { roc_auc: 0.5 } }, approval_id: null, previous_champion_id: null,
    promoted_by: status === "champion" ? "usr_approver" : null, reason: null, created_by: "usr_admin", promoted_at: status === "champion" ? T : null,
    retired_at: null, created_at: T, ...over };
}

function experiment(id: string, def: string, v: number, over: Partial<MLExperiment> = {}): MLExperiment {
  return {
    id, workspace_id: "ws_demo", run_id: null, definition_id: def, definition_key: MODEL, definition_version: v, task: "classify",
    spec_hash: `sh_${id}`, status: "succeeded", verdict: "improved", dataset_asset: "stg_sn.incident", dataset_source_id: "src_sn",
    dataset_version: `dv_${id}_9f86d081884c7d65`, split_id: `spl_${id}`, manifest_hash: MANIFEST_NEW, selection_hash: `sel_${id}_0a1b2c3d4e`,
    evaluation_seal: `seal_${id}_77aa11bb22`, package_hash: PKG_NEW, code_digest: "cd_1", environment_digest: "env_1",
    readiness: { ok: true, checks: [
      { check: "target_present", outcome: "pass", reason: "breached_sla is present for 95% of rows" },
      { check: "post_cutoff_timestamps", outcome: "pass", reason: "every feature timestamp is at or before the cutoff" },
      { check: "label_maturity", outcome: "pass", reason: "210 rows with immature labels excluded (horizon 14 days)" },
      { check: "entity_leakage", outcome: "pass", reason: "no assignment group crosses train and holdout" },
    ], problems: [], excluded: { immature_labels: 210 }, usable_rows: 4000 },
    artifacts: { ml_spec: `art_${id}_spec`, ml_split_manifest: `art_${id}_split`, ml_trials: `art_${id}_trials`, ml_model: `art_${id}_model`,
      ml_evaluation: `art_${id}_eval`, ml_model_card: `art_${id}_card` },
    verification_record_id: `ver_${id}`, query_ids: ["qry_ml_1"], reproduction_of: null, error: null, created_by: "usr_admin", created_at: T, finished_at: T,
    summary: { metric: "roc_auc", baseline: { roc_auc: 0.5 }, candidate: { roc_auc: 0.774, precision: 0.61, recall: 0.58 },
      decision: { improved: true, gain: 0.274, min_improvement: 0.02, reason: "the candidate beats the baseline by 0.274 roc_auc (≥ 0.02)" },
      estimator: "gradient_boosting", trials_run: 3, stopped: null, seconds: 42, rows_read: 4210 },
    verification: verified(id) as MLExperiment["verification"],
    ...over,
  };
}

const records = (exp: MLExperiment, auc: number) => ({
  ml_split_manifest: { experiment_id: exp.id, split_id: exp.split_id, manifest_hash: exp.manifest_hash, membership_hash: "mem_1",
    manifest: { version: "ml_split.v1", dataset_version: exp.dataset_version, strategy: "chronological", seed: 7, holdout_fraction: 0.2,
      embargo_periods: 1, independence_justification: null, time_column: "opened_at", group_columns: [],
      rows: { usable: 4000, train: 3150, holdout: 800, folds: [{ train: 1050, validation: 700 }, { train: 1750, validation: 700 }, { train: 2450, validation: 700 }] },
      boundaries: { holdout_from: "2026-08-01T00:00:00", embargo_from: "2026-07-25T00:00:00", holdout_periods: 8, distinct_periods: 40 },
      groups: {}, excluded: { immature_labels: 210 } } },
  ml_trials: { experiment_id: exp.id, selection_hash: exp.selection_hash, stopped: null, caps: { max_trials: 12, trials_run: 3 },
    selection: { trial: 2, estimator: "gradient_boosting", params: { max_depth: 3 }, cv_mean: 0.781, baseline_cv_mean: 0.5, metric: "roc_auc",
      manifest_hash: exp.manifest_hash, folds: 3 },
    trials: [
      { trial: 0, role: "baseline", estimator: "dummy_prior", params: {}, status: "succeeded", error: null, folds: [0.5, 0.5, 0.5], mean: 0.5 },
      { trial: 1, role: "candidate", estimator: "logistic", params: { C: 1 }, status: "succeeded", error: null, folds: [0.7, 0.71, 0.72], mean: 0.71 },
      { trial: 2, role: "candidate", estimator: "gradient_boosting", params: { max_depth: 3 }, status: "succeeded", error: null, folds: [0.77, 0.78, 0.793], mean: 0.781 },
      { trial: 3, role: "candidate", estimator: "random_forest", params: { n_estimators: 200 }, status: "failed", error: "time cap reached", folds: null, mean: null },
    ] },
  ml_evaluation: { experiment_id: exp.id, seal: exp.evaluation_seal, sealed: true, report: {
    metric: "roc_auc", baseline: { roc_auc: 0.5 }, candidate: { roc_auc: auc, precision: 0.61, recall: 0.58 },
    decision: exp.summary.decision, uncertainty: { resamples: 200, level: 0.95, ci_low: 0.221, ci_high: 0.318, p_better: 1 },
    holdout: { rows: 800, read_after_selection: exp.selection_hash }, threshold: { threshold: 0.42, expected_cost_per_row: 0.8 },
    checks: [
      { check: "baseline_same_splits", outcome: "pass", reason: "baseline and every candidate were scored on the manifest's 3 folds and the same 800 holdout rows" },
      { check: "holdout_untouched_until_selection", outcome: "pass", reason: `selection ${String(exp.selection_hash).slice(0, 12)} froze before the holdout was read` },
      { check: "improvement_over_baseline", outcome: "pass", reason: String(exp.summary.decision?.reason ?? "") },
    ], verdict: exp.verdict } },
  ml_model_card: { version: "ml_model_card.v1", experiment_id: exp.id, verdict: exp.verdict, prose_source: "template", bound: true,
    facts: [{ id: "f1", label: "candidate holdout roc_auc", value: auc, unit: null, source: "evaluation/candidate/roc_auc" },
      { id: "f2", label: "holdout rows", value: 800, unit: "rows", source: "evaluation/../manifest/rows/holdout" }],
    markdown: `# Model card: ${MODEL}\n\n## Intended use\nBatch predictions of \`breached_sla\` from \`stg_sn.incident\`.\n\n## Performance\nHoldout roc_auc: candidate ${auc}, baseline 0.5.\n\n## Limitations\n- Feature importance explains the model's behaviour, not causality.` },
});

interface MlState {
  defs: Record<string, DefinitionVersion>;
  experiments: MLExperiment[];
  models: ModelVersion[];
  scoring: ScoringRun[];
  monitors: Monitor[];
}
let s: MlState;

function seed(): MlState {
  const old = experiment("mlx_0", "defn_ml0", 1, { manifest_hash: MANIFEST_OLD, package_hash: PKG_OLD,
    summary: { ...experiment("x", "d", 1).summary, candidate: { roc_auc: 0.74 }, decision: { improved: true, gain: 0.24, min_improvement: 0.02, reason: "beats the baseline" } } });
  const drift: Monitor = { id: "mon_ml_drift", workspace_id: "ws_demo", name: "p1_breach input drift", kind: "ml_drift",
    config: { model: MODEL, psi_threshold: 0.2 } as Monitor["config"], enabled: true, auto_investigate: false, state: "alerting", last_evaluated_at: T,
    last_result: { psi: 0.31, worst_feature: "assignment_group", message: "input drift PSI 0.31 > 0.2 on assignment_group; drift alone does not show a loss of performance" } as Monitor["last_result"],
    created_by: "usr_admin", created_at: T };
  return { defs: {}, experiments: [old], models: [version("mlv_0a", 1, "mlx_0", "champion", PKG_OLD, 0.74)], scoring: [], monitors: [drift] };
}
s = seed();

export function resetMl(): void {
  s = seed();
}

export const mlMonitors = (): Monitor[] => s.monitors;

export function proposal(body: { asset?: string; target?: string; task?: string; objective?: string }): MLProposal {
  if (!body.asset) return { proposal: null, problems: ["name asset"], source: "inputs" };
  const forecast = body.task === "forecast";
  return {
    proposal: forecast ? {
      type: "ml", task: "forecast", dataset: { asset: body.asset }, target: body.target || "incidents", time_column: "opened_at", horizon: 8, season_length: 7,
      split: { strategy: "chronological", holdout_fraction: 0.2, validation_folds: 3, embargo_periods: 0 }, estimators: ["seasonal_naive", "ets"], objective_metric: "mae",
    } : {
      type: "ml", task: "classify", dataset: { asset: body.asset }, target: body.target || "breached_sla", positive_class: true, entity_keys: ["sys_id"],
      time_column: "opened_at", cutoff_column: "opened_at", outcome_time_column: "resolved_at", label_horizon: 14,
      features: [{ column: "priority", available_at: "cutoff" }, { column: "assignment_group", available_at: "cutoff" },
        { column: "reassignment_count", available_at: "cutoff" }],
      split: { strategy: "chronological", holdout_fraction: 0.2, validation_folds: 3, embargo_periods: 1 },
      estimators: ["dummy_prior", "logistic", "gradient_boosting", "random_forest"], objective_metric: "roc_auc", min_improvement: 0.02,
      search: { max_trials: 12, max_seconds: 300, max_rows: 100000 },
    },
    problems: [], source: "rules",
  };
}

function def(body: { kind: string; key: string; spec: Record<string, unknown>; title?: string | null }): DefinitionVersion {
  const n = Object.values(s.defs).filter((d) => d.key === body.key).length + 1;
  const id = `defn_${body.kind === "ml_spec" ? "ml" : "sc"}${Object.keys(s.defs).length + 1}`;
  const d: DefinitionVersion = { id, workspace_id: "ws_demo", kind: body.kind, key: body.key, version: n, status: "draft", title: body.title ?? null,
    content_hash: `c_${id}`, revision: 1, created_by: "usr_admin", published_by: null, retired_by: null, reason: null, published_at: null, retired_at: null,
    created_at: T, updated_at: T, spec: body.spec };
  s.defs[id] = d;
  return d;
}

function train(defId: string): Reply {
  const d = s.defs[defId];
  if (!d || d.status !== "published") return err(422, "invalid_input", "only a published ml_spec version trains");
  const spec = d.spec as { features?: { column: string; available_at?: string }[] };
  // declared after the outcome, or (the server's timestamp check) observed after the prediction cutoff
  const leaky = (spec.features ?? []).filter((f) => f.available_at === "after_outcome" || f.column === "resolved_at");
  const cols = leaky.map((f) => f.column).join(", ");
  const id = `mlx_${s.experiments.length}`;
  if (leaky.length) {
    const refused = experiment(id, defId, d.version, { status: "refused", verdict: null, split_id: null, manifest_hash: null, selection_hash: null,
      evaluation_seal: null, package_hash: null, artifacts: {}, verification_record_id: null, verification: undefined,
      error: `post_cutoff_timestamps: feature values observed after the prediction cutoff: ${cols} (812 rows)`,
      summary: { rows_read: 4210 }, readiness: { ok: false, problems: [`post_cutoff_timestamps: feature values observed after the prediction cutoff: ${cols}`], checks: [
        { check: "target_present", outcome: "pass", reason: "breached_sla is present for 95% of rows" },
        { check: "feature_availability", outcome: "pass", reason: "no feature is declared known only after the outcome" },
        { check: "post_cutoff_timestamps", outcome: "fail", reason: `feature values observed after the prediction cutoff: ${cols} (812 rows)` },
      ], excluded: {}, usable_rows: 0 } });
    s.experiments.unshift(refused);
    return err(422, "invalid_input", `the experiment was refused before training: post_cutoff_timestamps: feature values observed after the prediction cutoff: ${cols}`,
      { experiment_id: id, readiness: refused.readiness });
  }
  const exp = experiment(id, defId, d.version);
  const mv = version("mlv_1b", 2, id, "challenger", PKG_NEW, 0.774);
  s.experiments.unshift(exp);
  s.models = [mv, ...s.models.filter((m) => m.id !== mv.id)];
  return ok({ ...exp, model_version: mv }, 201);
}

function promote(versionId: string, approvalId: string | null | undefined): Reply {
  const mv = s.models.find((m) => m.id === versionId);
  if (!mv) return err(404, "not_found", "model version not found");
  if (!approvalId) {
    const a = requestApproval("apr_promote", "ml.promote", `model ${MODEL}`, { model: MODEL, version_id: mv.id, version: mv.version, package_hash: mv.package_hash,
      replaces: s.models.find((m) => m.status === "champion")?.id ?? null }, ["stg_sn.incident"]);
    return ok({ status: "approval_required", approval_id: a.id, payload_hash: a.payload_hash, expires_at: a.expires_at });
  }
  if (approvalStatus(approvalId) !== "approved") return err(409, "approval_required", "the promotion approval is not approved yet");
  const champ = s.models.find((m) => m.status === "champion");
  s.models = s.models.map((m) => (m.id === mv.id ? { ...m, status: "champion", previous_champion_id: champ?.id ?? null, promoted_at: T, approval_id: approvalId }
    : m.id === champ?.id ? { ...m, status: "retired", retired_at: T } : m));
  return ok({ status: "promoted", model_version: s.models.find((m) => m.id === mv.id) });
}

function rollback(approvalId: string | null | undefined): Reply {
  const champ = s.models.find((m) => m.status === "champion");
  if (!champ?.previous_champion_id) return err(409, "conflict", "no earlier champion to restore");
  if (!approvalId) {
    const a = requestApproval("apr_rollback", "ml.rollback", `model ${MODEL}`, { model: MODEL, from: champ.id, to: champ.previous_champion_id });
    return ok({ status: "approval_required", approval_id: a.id, payload_hash: a.payload_hash });
  }
  if (approvalStatus(approvalId) !== "approved") return err(409, "approval_required", "the rollback approval is not approved yet");
  const prev = champ.previous_champion_id;
  s.models = s.models.map((m) => (m.id === champ.id ? { ...m, status: "retired", retired_at: T } : m.id === prev ? { ...m, status: "champion" } : m));
  return ok({ status: "rolled_back", model_version: s.models.find((m) => m.id === prev), retired: s.models.find((m) => m.id === champ.id) });
}

function planScoring(defId: string): Reply {
  const d = s.defs[defId];
  if (!d || d.status !== "published") return err(422, "invalid_input", "only a published ml_scoring version scores");
  const spec = d.spec as { model_version_id: string; input?: { asset: string }; output: string };
  const champ = s.models.find((m) => m.status === "champion");
  if (spec.model_version_id !== champ?.id) return err(409, "conflict", "the pinned model version is not the champion; publish a new scoring definition");
  const done = s.scoring.find((r) => r.definition_id === defId && r.status === "succeeded");
  if (done) return ok({ status: "duplicate", scoring_run_id: done.id, scoring_run: done });
  const id = `scr_${s.scoring.length + 1}`;
  const a = requestApproval(`apr_score_${s.scoring.length + 1}`, "ml.score", `ml_${spec.output}`, { definition: d.key, model_version_id: spec.model_version_id,
    input: spec.input?.asset, output: spec.output }, [spec.input?.asset ?? ""]);
  s.scoring.unshift({ id, workspace_id: "ws_demo", definition_id: defId, definition_key: d.key, definition_version: d.version,
    model_version_id: spec.model_version_id, package_hash: champ.package_hash, input_asset: spec.input?.asset ?? "", input_version: "dv_input_1",
    status: "awaiting_approval", approval_id: a.id, rows_input: null, rows_scored: null, rows_rejected: null, output_table: null, rejected_table: null,
    details: {}, error: null, created_by: "usr_admin", created_at: T, finished_at: null });
  return ok({ status: "approval_required", scoring_run_id: id, approval_id: a.id });
}

function execute(id: string, approvalId: string | null | undefined): Reply {
  const r = s.scoring.find((x) => x.id === id);
  if (!r) return err(404, "not_found", "scoring run not found");
  if (!approvalId || approvalStatus(approvalId) !== "approved") return err(409, "approval_required", "the scoring approval is not approved yet");
  const done: ScoringRun = { ...r, status: "succeeded", rows_input: 4210, rows_scored: 4198, rows_rejected: 12,
    output_table: `aos_out.ml_${(s.defs[r.definition_id].spec as { output: string }).output}`,
    rejected_table: `aos_out.ml_${(s.defs[r.definition_id].spec as { output: string }).output}_rejected`,
    details: { rejected_reasons: { missing_feature: 12 } }, finished_at: T };
  s.scoring = s.scoring.map((x) => (x.id === id ? done : x));
  return ok({ status: "succeeded", scoring_run: done });
}

export function mlRoute(m: string, p: string, url: URL, W: string, body: Record<string, unknown>): Reply {
  if (m === "POST" && p === `${W}/ml/proposals`) return ok(proposal(body as { asset?: string }));
  if (m === "POST" && p === `${W}/definitions` && (body.kind === "ml_spec" || body.kind === "ml_scoring")) {
    return ok(def(body as { kind: string; key: string; spec: Record<string, unknown> }), 201);
  }
  const pub = new RegExp(`^${W}/definitions/(defn_(?:ml|sc)\\d+)/publish$`).exec(p);
  if (m === "POST" && pub) {
    const d = s.defs[pub[1]];
    s.defs[pub[1]] = { ...d, status: "published", revision: d.revision + 1, published_at: T, published_by: "usr_admin" };
    return ok(s.defs[pub[1]]);
  }
  if (p === `${W}/ml/experiments`) {
    if (m === "GET") return ok(s.experiments);
    if (m === "POST") return train(String(body.definition));
  }
  const ex = new RegExp(`^${W}/ml/experiments/([^/]+)(?:/records/([^/]+))?$`).exec(p);
  if (m === "GET" && ex) {
    const exp = s.experiments.find((e) => e.id === ex[1]);
    if (!exp) return err(404, "not_found", "experiment not found");
    if (!ex[2]) return ok({ ...exp, model_version: s.models.find((x) => x.experiment_id === exp.id) });
    const recs = records(exp, Number(exp.summary.candidate?.roc_auc ?? 0.77)) as Record<string, unknown>;
    if (!exp.artifacts?.[ex[2]] || !recs[ex[2]]) return err(404, "not_found", `experiment ${exp.id} has no ${ex[2]} record`);
    return ok({ artifact_id: exp.artifacts[ex[2]], type: ex[2], version: 1, content_hash: `ch_${ex[2]}`, content: recs[ex[2]] });
  }
  if (m === "GET" && p === `${W}/ml/models`) {
    const name = url.searchParams.get("name");
    return ok(s.models.filter((x) => !name || x.name === name));
  }
  const pr = new RegExp(`^${W}/ml/models/([^/]+)/promote$`).exec(p);
  if (m === "POST" && pr) return promote(pr[1], body.approval_id as string | null);
  if (m === "POST" && p === `${W}/ml/model-names/${MODEL}/rollback`) return rollback(body.approval_id as string | null);
  if (p === `${W}/ml/scoring`) {
    if (m === "GET") return ok(s.scoring);
    if (m === "POST") return planScoring(String(body.definition));
  }
  const sx = new RegExp(`^${W}/ml/scoring/([^/]+)/execute$`).exec(p);
  if (m === "POST" && sx) return execute(sx[1], body.approval_id as string | null);
  if (m === "POST" && p === `${W}/monitors` && String(body.kind ?? "").startsWith("ml_")) {
    const mon: Monitor = { id: `mon_ml_${s.monitors.length + 1}`, workspace_id: "ws_demo", name: String(body.name), kind: String(body.kind),
      config: body.config as Monitor["config"], enabled: true, auto_investigate: false, state: "ok", last_evaluated_at: null, last_result: {},
      created_by: "usr_admin", created_at: T };
    s.monitors.push(mon);
    return ok(mon, 201);
  }
  return null;
}
