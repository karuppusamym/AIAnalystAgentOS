import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { ApiError, api, downloadFile, saveBlob, type ApprovalStep, type MLExperiment, type MLProposal, type MLSpecDoc, type MLTrial,
  type ModelVersion, type ScoringRun, type SplitManifest } from "../api";
import { fmtDate, fmtNumber, fmtValue, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast } from "../routes";
import { ApprovalStepper } from "./ApprovalStepper";
import { Markdown } from "./Markdown";
import { VerificationBadge } from "./WhyNumber";
import { Card, EmptyState, ErrorBox, Field, KeyValue, Loading, Notice, StatusBadge, Tag, TechnicalDetails } from "./ui";

// Mirrors contracts/work.py: the estimator allowlist (the first is the mandatory baseline) and metrics per task.
export const ESTIMATORS: Record<string, string[]> = {
  classify: ["dummy_prior", "logistic", "gradient_boosting", "random_forest"],
  regress: ["dummy_mean", "linear", "gradient_boosting", "random_forest"],
  forecast: ["seasonal_naive", "ets", "arima"],
  cluster: ["random_partition", "kmeans", "gmm"],
  anomaly: ["robust_z", "seasonal_residual", "isolation_forest"],
};
export const METRICS: Record<string, string[]> = {
  classify: ["roc_auc", "log_loss", "balanced_accuracy", "f1", "accuracy"], regress: ["mae", "rmse", "r2"], forecast: ["mae", "rmse"],
  cluster: ["silhouette"], anomaly: ["f1", "recall", "precision"],
};
const LOWER_IS_BETTER = new Set(["log_loss", "mae", "rmse"]);
export const direction = (metric: string | null | undefined) => (metric && LOWER_IS_BETTER.has(metric) ? "lower is better" : "higher is better");

const VERDICT_WORDS: Record<string, { label: string; status: string }> = {
  improved: { label: "beats its baseline", status: "verified" }, no_improvement: { label: "no improvement over its baseline", status: "inconclusive" },
  guardrail_failed: { label: "a slice guardrail failed", status: "failed" }, invalid: { label: "invalid evaluation", status: "failed" },
};
const TASK_WORDS: Record<string, string> = { classify: "classification", regress: "regression", forecast: "forecast", cluster: "clustering", anomaly: "anomaly detection" };

const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const num = (s: string, d?: number) => (s.trim() === "" ? d : Number(s));

// ------------------------------------------------------------------------------------ MLSpec form
interface SpecDraft {
  key: string;
  task: string;
  asset: string;
  target: string;
  positiveClass: string;
  timeColumn: string;
  cutoffColumn: string;
  outcomeTimeColumn: string;
  labelHorizon: string;
  horizon: string;
  entityKeys: string;
  features: { column: string; available_at: string }[];
  strategy: string;
  holdout: string;
  folds: string;
  embargo: string;
  justification: string;
  estimators: string[];
  metric: string;
  minImprovement: string;
  maxTrials: string;
}

export function draftFromSpec(spec: MLSpecDoc, key: string): SpecDraft {
  const task = String(spec.task);
  return {
    key, task, asset: spec.dataset?.asset ?? "", target: spec.target ?? "", positiveClass: spec.positive_class == null ? "" : String(spec.positive_class),
    timeColumn: spec.time_column ?? "", cutoffColumn: spec.cutoff_column ?? "", outcomeTimeColumn: spec.outcome_time_column ?? "",
    labelHorizon: spec.label_horizon == null ? "" : String(spec.label_horizon), horizon: spec.horizon == null ? "" : String(spec.horizon),
    entityKeys: (spec.entity_keys ?? []).join(", "), features: (spec.features ?? []).map((f) => ({ column: f.column, available_at: String(f.available_at ?? "cutoff") })),
    strategy: spec.split?.strategy ?? (task === "forecast" ? "chronological" : "chronological"), holdout: String(spec.split?.holdout_fraction ?? 0.2),
    folds: String(spec.split?.validation_folds ?? 3), embargo: String(spec.split?.embargo_periods ?? 0), justification: spec.split?.independence_justification ?? "",
    estimators: spec.estimators?.length ? spec.estimators : ESTIMATORS[task] ?? [], metric: spec.objective_metric ?? METRICS[task]?.[0] ?? "",
    minImprovement: String(spec.min_improvement ?? 0), maxTrials: String(spec.search?.max_trials ?? 12),
  };
}

/** The draft as an MLSpec (contracts/work.py); the server validates it again before anything trains. */
export function specFromDraft(d: SpecDraft): MLSpecDoc {
  const baseline = ESTIMATORS[d.task]?.[0];
  const estimators = [...new Set([baseline, ...d.estimators].filter((e): e is string => !!e))];
  const pc = d.positiveClass.trim();
  return {
    type: "ml", task: d.task, dataset: { asset: d.asset.trim() }, target: d.target.trim() || null,
    positive_class: pc === "" ? null : pc === "true" ? true : pc === "false" ? false : Number.isFinite(Number(pc)) ? Number(pc) : pc,
    time_column: d.timeColumn.trim() || null, cutoff_column: d.cutoffColumn.trim() || null, outcome_time_column: d.outcomeTimeColumn.trim() || null,
    label_horizon: num(d.labelHorizon) ?? null, horizon: d.task === "forecast" ? num(d.horizon) ?? null : null, entity_keys: list(d.entityKeys),
    features: d.features.filter((f) => f.column.trim()).map((f) => ({ column: f.column.trim(), available_at: f.available_at })),
    split: { strategy: d.strategy, holdout_fraction: num(d.holdout, 0.2)!, validation_folds: num(d.folds, 3)!, embargo_periods: num(d.embargo, 0)!,
      independence_justification: d.justification.trim() || null },
    estimators, objective_metric: d.metric || null, min_improvement: num(d.minImprovement, 0)!, search: { max_trials: num(d.maxTrials, 12)! },
  };
}

/** Problems the form can see before sending (the server checks everything again). */
export function draftProblems(d: SpecDraft): string[] {
  const p: string[] = [];
  if (!/^[a-z][a-z0-9_]{0,55}$/.test(d.key)) p.push("Name: lowercase letters, digits and underscores, starting with a letter.");
  if (!d.asset.trim()) p.push("Choose the table to learn from.");
  if ((d.task === "classify" || d.task === "regress" || d.task === "forecast") && !d.target.trim()) p.push("Name the target column.");
  if (d.task === "forecast" && !d.timeColumn.trim()) p.push("A forecast needs a time column.");
  if (d.strategy === "random" && !d.justification.trim()) p.push("A random split needs a reason the rows are independent; otherwise use a chronological or group split.");
  if (d.features.some((f) => f.available_at === "after_outcome")) {
    p.push("A feature known only after the outcome is leakage: the server refuses to train with it.");
  }
  return p;
}

function MLSpecForm({ wsId, kind, onStarted, onRefused, onCancel }: {
  wsId: string; kind: "predict" | "forecast"; onStarted: (e: MLExperiment) => void; onRefused: (experimentId: string) => void; onCancel: () => void;
}) {
  const id = useId();
  const catalog = useAsync(() => api.catalog(wsId), [wsId]);
  const [asset, setAsset] = useState("");
  const [objective, setObjective] = useState("");
  const [target, setTarget] = useState("");
  const [proposal, setProposal] = useState<MLProposal | null>(null);
  const [d, setD] = useState<SpecDraft | null>(null);
  const [stage, setStage] = useState<string | null>(null);
  const act = useAction();
  const columns = catalog.data?.find((a) => a.fq === asset)?.columns.map((c) => c.name) ?? [];
  const set = (patch: Partial<SpecDraft>) => setD((x) => (x ? { ...x, ...patch } : x));

  const propose = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.mlPropose(wsId, { asset: asset.trim(), objective: objective.trim() || null, target: target.trim() || null,
      task: kind === "forecast" ? "forecast" : null }));
    if (r) {
      setProposal(r);
      const key = (r.proposal?.target ? `${asset.split(".").pop()}_${r.proposal.target}` : `${asset.split(".").pop()}_model`).toLowerCase().replace(/[^a-z0-9_]/g, "_");
      setD(draftFromSpec(r.proposal ?? { task: kind === "forecast" ? "forecast" : "classify", dataset: { asset } }, key.slice(0, 55)));
    }
  };
  const train = async (e: FormEvent) => {
    e.preventDefault();
    if (!d || draftProblems(d).length) return;
    const spec = specFromDraft(d);
    try {
      setStage("Saving the spec as a draft definition…");
      const r = await act.run(async () => {
        const draft = await api.createDefinition(wsId, { kind: "ml_spec", key: d.key, spec: spec as Record<string, unknown>, title: objective.trim() || null });
        setStage("Publishing it (only a published spec trains)…");
        const published = await api.publishDefinition(wsId, draft.id, draft.revision);
        setStage("Checking readiness and leakage, splitting, then training against the baseline…");
        return api.startExperiment(wsId, published.id);
      });
      if (r) onStarted(r);
    } finally {
      setStage(null);
    }
  };
  // a refused experiment carries its id in the error details: open its readiness and leakage result
  useEffect(() => {
    const f = act.failure;
    if (f instanceof ApiError && typeof f.details.experiment_id === "string") onRefused(f.details.experiment_id);
  }, [act.failure]); // eslint-disable-line react-hooks/exhaustive-deps

  const problems = d ? draftProblems(d) : [];
  return (
    <Card title={kind === "forecast" ? "New forecast" : "New prediction model"} label="ML spec">
      <p className="muted small">Models propose the target, features and estimators; code checks readiness and leakage, splits, trains against a
        mandatory baseline and reads the holdout once. Nothing is promoted or scored without an approval.</p>
      <form className="form" onSubmit={propose} aria-label="What to predict">
        <div className="form-row">
          <Field label="Table" htmlFor={`${id}-asset`} hint="schema.table in your scope.">
            <input id={`${id}-asset`} list={`${id}-assets`} value={asset} onChange={(e) => setAsset(e.target.value)} placeholder="stg_sn.incident" />
            <datalist id={`${id}-assets`}>{(catalog.data ?? []).map((a) => <option key={a.fq} value={a.fq} />)}</datalist>
          </Field>
          <Field label={kind === "forecast" ? "Series to forecast" : "Target column"} htmlFor={`${id}-target`} hint="Optional: the proposal suggests one.">
            <input id={`${id}-target`} list={`${id}-cols`} value={target} onChange={(e) => setTarget(e.target.value)} />
            <datalist id={`${id}-cols`}>{columns.map((c) => <option key={c} value={c} />)}</datalist>
          </Field>
        </div>
        <Field label="What should it predict, and why?" htmlFor={`${id}-obj`}>
          <input id={`${id}-obj`} value={objective} onChange={(e) => setObjective(e.target.value)} placeholder="Which P1 incidents will breach their SLA?" />
        </Field>
        <div className="form-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-sm" disabled={act.busy || !asset.trim()}>{d ? "Propose again" : "Propose a spec"}</button>
        </div>
      </form>
      {proposal && (
        <p className="small">Proposed by {proposal.source === "rules" ? "the rules (no model call)" : proposal.source}.
          {proposal.problems.length > 0 && <> Problems: {proposal.problems.join("; ")}</>}</p>
      )}
      {d && (
        <form className="form" onSubmit={train} aria-label="ML spec">
          <div className="form-row">
            <Field label="Name" htmlFor={`${id}-key`} hint="The model's name; each save is a new version.">
              <input id={`${id}-key`} value={d.key} onChange={(e) => set({ key: e.target.value })} />
            </Field>
            <Field label="Task" htmlFor={`${id}-task`}>
              <select id={`${id}-task`} value={d.task} onChange={(e) => set({ task: e.target.value, estimators: ESTIMATORS[e.target.value] ?? [], metric: METRICS[e.target.value]?.[0] ?? "" })}>
                {(kind === "forecast" ? ["forecast"] : ["classify", "regress", "anomaly", "cluster"]).map((t) => <option key={t} value={t}>{TASK_WORDS[t]}</option>)}
              </select>
            </Field>
          </div>
          <div className="form-row">
            <Field label="Target" htmlFor={`${id}-t2`}><input id={`${id}-t2`} value={d.target} onChange={(e) => set({ target: e.target.value })} /></Field>
            {d.task === "classify" && <Field label="Positive class" htmlFor={`${id}-pc`}><input id={`${id}-pc`} value={d.positiveClass} onChange={(e) => set({ positiveClass: e.target.value })} /></Field>}
            {d.task === "forecast" && <Field label="Horizon (steps)" htmlFor={`${id}-h`}><input id={`${id}-h`} inputMode="numeric" value={d.horizon} onChange={(e) => set({ horizon: e.target.value })} /></Field>}
          </div>
          <fieldset className="schema-fieldset">
            <legend>Time and label availability</legend>
            <div className="form-row">
              <Field label="Time column" htmlFor={`${id}-tc`}><input id={`${id}-tc`} value={d.timeColumn} onChange={(e) => set({ timeColumn: e.target.value })} /></Field>
              <Field label="Prediction cutoff column" htmlFor={`${id}-cc`} hint="Features must be known at or before it.">
                <input id={`${id}-cc`} value={d.cutoffColumn} onChange={(e) => set({ cutoffColumn: e.target.value })} /></Field>
            </div>
            <div className="form-row">
              <Field label="Outcome time column" htmlFor={`${id}-oc`}><input id={`${id}-oc`} value={d.outcomeTimeColumn} onChange={(e) => set({ outcomeTimeColumn: e.target.value })} /></Field>
              <Field label="Label horizon (days)" htmlFor={`${id}-lh`} hint="Rows whose label is not mature yet are excluded.">
                <input id={`${id}-lh`} inputMode="numeric" value={d.labelHorizon} onChange={(e) => set({ labelHorizon: e.target.value })} /></Field>
            </div>
            <Field label="Entity keys" htmlFor={`${id}-ek`}><input id={`${id}-ek`} value={d.entityKeys} onChange={(e) => set({ entityKeys: e.target.value })} /></Field>
          </fieldset>
          {d.task !== "forecast" && (
            <fieldset className="schema-fieldset">
              <legend>Features and when each is known</legend>
              <ul className="assertion-list" aria-label="Features">
                {d.features.map((f, i) => (
                  <li key={i} className="form-row">
                    <Field label={`Feature ${i + 1}`} htmlFor={`${id}-f${i}`}>
                      <input id={`${id}-f${i}`} value={f.column} onChange={(e) => set({ features: d.features.map((x, j) => (j === i ? { ...x, column: e.target.value } : x)) })} />
                    </Field>
                    <Field label={`Feature ${i + 1} is known`} htmlFor={`${id}-fa${i}`}>
                      <select id={`${id}-fa${i}`} value={f.available_at} onChange={(e) => set({ features: d.features.map((x, j) => (j === i ? { ...x, available_at: e.target.value } : x)) })}>
                        <option value="cutoff">at the prediction cutoff</option>
                        <option value="known_in_advance">in advance (e.g. a calendar)</option>
                        <option value="after_outcome">only after the outcome (leakage)</option>
                      </select>
                    </Field>
                  </li>
                ))}
              </ul>
              <button type="button" className="btn btn-xs" onClick={() => set({ features: [...d.features, { column: "", available_at: "cutoff" }] })}>Add a feature</button>
            </fieldset>
          )}
          <fieldset className="schema-fieldset">
            <legend>Validation</legend>
            <div className="form-row">
              <Field label="Split" htmlFor={`${id}-st`}>
                <select id={`${id}-st`} value={d.strategy} onChange={(e) => set({ strategy: e.target.value })}>
                  <option value="chronological">chronological (train on the past, hold out the latest)</option>
                  <option value="group">by group (no entity in both)</option>
                  <option value="group_chronological">by group and time</option>
                  <option value="random">random (needs a reason)</option>
                </select>
              </Field>
              <Field label="Holdout fraction" htmlFor={`${id}-hf`}><input id={`${id}-hf`} inputMode="decimal" value={d.holdout} onChange={(e) => set({ holdout: e.target.value })} /></Field>
              <Field label="Validation folds" htmlFor={`${id}-vf`}><input id={`${id}-vf`} inputMode="numeric" value={d.folds} onChange={(e) => set({ folds: e.target.value })} /></Field>
              <Field label="Embargo periods" htmlFor={`${id}-em`}><input id={`${id}-em`} inputMode="numeric" value={d.embargo} onChange={(e) => set({ embargo: e.target.value })} /></Field>
            </div>
            {d.strategy === "random" && (
              <Field label="Why are the rows independent?" htmlFor={`${id}-ij`}><input id={`${id}-ij`} value={d.justification} onChange={(e) => set({ justification: e.target.value })} /></Field>
            )}
            <div className="form-row">
              <Field label="Objective metric" htmlFor={`${id}-m`} hint={d.metric ? `${d.metric}: ${direction(d.metric)}` : undefined}>
                <select id={`${id}-m`} value={d.metric} onChange={(e) => set({ metric: e.target.value })}>
                  {(METRICS[d.task] ?? []).map((m) => <option key={m} value={m}>{m} ({direction(m)})</option>)}
                </select>
              </Field>
              <Field label="Minimum improvement" htmlFor={`${id}-mi`} hint="Over the baseline, in metric units.">
                <input id={`${id}-mi`} inputMode="decimal" value={d.minImprovement} onChange={(e) => set({ minImprovement: e.target.value })} /></Field>
              <Field label="Maximum trials" htmlFor={`${id}-mt`}><input id={`${id}-mt`} inputMode="numeric" value={d.maxTrials} onChange={(e) => set({ maxTrials: e.target.value })} /></Field>
            </div>
            <fieldset className="schema-fieldset">
              <legend>Estimators</legend>
              <div className="chip-row">
                {(ESTIMATORS[d.task] ?? []).map((e, i) => (
                  <label key={e} className="toggle small">
                    <input type="checkbox" checked={i === 0 || d.estimators.includes(e)} disabled={i === 0}
                      onChange={(ev) => set({ estimators: ev.target.checked ? [...d.estimators, e] : d.estimators.filter((x) => x !== e) })} />
                    {e}{i === 0 ? " (baseline, always trained)" : ""}
                  </label>
                ))}
              </div>
            </fieldset>
          </fieldset>
          {problems.length > 0 && <ul className="small warn-text" aria-label="Problems with the spec">{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
          {stage && <p className="small" role="status">{stage}</p>}
          <ErrorBox error={act.error} />
          <TechnicalDetails value={specFromDraft(d)} label="The MLSpec" />
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={act.busy || problems.length > 0}>{act.busy ? "Working…" : "Save, publish and train"}</button>
          </div>
        </form>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------------------------ experiment detail
/** The ML readiness and leakage checks, each with its outcome and reason (a refused experiment shows why). */
function MlReadiness({ exp }: { exp: MLExperiment }) {
  const checks = exp.readiness?.checks ?? [];
  if (!checks.length) return null;
  const failed = checks.filter((c) => (c.outcome ?? c.status) === "fail");
  const excluded = Object.entries((exp.readiness?.excluded ?? {}) as Record<string, number>);
  return (
    <section className="card" aria-label="Readiness and leakage checks">
      <div className="card-body stack">
        <h3 className="h-sm">Readiness and leakage</h3>
        {failed.length ? <Notice tone="danger">Refused before training: {failed.length} check{failed.length > 1 ? "s" : ""} failed. Nothing was trained and no holdout was read.</Notice>
          : <p className="small">Every check passed before the data was split.</p>}
        <div className="table-wrap" tabIndex={0}>
          <table className="table table-compact">
            <caption className="sr-only">Readiness and leakage checks</caption>
            <thead><tr><th scope="col">Check</th><th scope="col">Result</th><th scope="col">Why</th></tr></thead>
            <tbody>
              {checks.map((c, k) => (
                <tr key={`${c.check}-${k}`}><td>{c.check.replace(/_/g, " ")}</td><td><StatusBadge status={String(c.outcome ?? c.status)} /></td><td className="small">{c.reason}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        {excluded.length > 0 && <p className="small">Excluded rows: {excluded.map(([k, v]) => `${fmtNumber(v, 0)} ${k.replace(/_/g, " ")}`).join(", ")}.</p>}
      </div>
    </section>
  );
}

/** The split as a diagram with a text equivalent: training, embargo and holdout, and each validation fold. */
export function SplitDiagram({ m, hash }: { m: SplitManifest; hash: string | null }) {
  const { train, holdout, usable, folds } = m.rows;
  const total = Math.max(1, train + holdout);
  const pct = (n: number) => `${Math.max(4, (100 * n) / total)}%`;
  const embargo = (m.embargo_periods ?? 0) > 0;
  const description = `${m.strategy} split (seed ${m.seed ?? "—"}): ${fmtNumber(train, 0)} training rows${embargo ? `, an embargo of ${m.embargo_periods} period${m.embargo_periods === 1 ? "" : "s"}` : ""}`
    + ` and ${fmtNumber(holdout, 0)} holdout rows${m.boundaries?.holdout_from ? ` from ${fmtDate(m.boundaries.holdout_from)}` : ""}; ${folds.length} validation folds inside the training rows.`;
  return (
    <figure className="stack" aria-label="Split diagram">
      <div className="split-diagram" role="img" aria-label={description}>
        <span className="split-seg split-train" style={{ width: pct(train) }}>train {fmtNumber(train, 0)}</span>
        {embargo && <span className="split-seg split-embargo" style={{ width: "6%" }}>embargo</span>}
        <span className="split-seg split-holdout" style={{ width: pct(holdout) }}>holdout {fmtNumber(holdout, 0)}</span>
      </div>
      <ol className="folds" aria-label="Validation folds">
        {folds.map((f, i) => (
          <li key={i} className="small">
            <span className="muted">Fold {i + 1}: train {fmtNumber(f.train, 0)}, validate {fmtNumber(f.validation, 0)}</span>
            <div className="fold-bar" aria-hidden="true">
              <span className="fold-train" style={{ width: `${(100 * f.train) / Math.max(1, train)}%` }} />
              <span className="fold-validation" style={{ width: `${(100 * f.validation) / Math.max(1, train)}%` }} />
            </div>
          </li>
        ))}
      </ol>
      <figcaption className="small">{description} {fmtNumber(usable, 0)} usable rows. Manifest <code>{shortHash(hash, 12)}</code>
        {m.dataset_version ? <> on data version <code>{shortHash(m.dataset_version, 12)}</code></> : null}.
        {m.groups?.overlap !== undefined && <> Groups in both train and holdout: {m.groups.overlap}.</>}</figcaption>
    </figure>
  );
}

/**
 * The baseline and candidate trials: every score is a cross-validation fold of this experiment's one
 * split, so the table states the split, its version and the metric direction together, and it is in
 * trial order, not a ranking.
 */
export function TrialsTable({ trials, metric, manifestHash, datasetVersion, selected }: {
  trials: MLTrial[]; metric: string; manifestHash: string | null; datasetVersion: string | null; selected: number | null;
}) {
  return (
    <div className="table-wrap" tabIndex={0}>
      <table className="table table-compact">
        <caption className="cap">Baseline and candidates on split <code>{shortHash(manifestHash, 12)}</code> (data {shortHash(datasetVersion, 10)}) ·
          {" "}{metric}, {direction(metric)} · validation folds only</caption>
        <thead><tr><th scope="col">Trial</th><th scope="col">Role</th><th scope="col">Estimator</th><th scope="col">Fold scores</th>
          <th scope="col" className="num">Mean {metric}</th><th scope="col">Status</th></tr></thead>
        <tbody>
          {trials.map((t) => (
            <tr key={t.trial} aria-current={t.trial === selected ? "true" : undefined}>
              <td>{t.trial}</td>
              <td>{t.role === "baseline" ? <Tag tone="info">baseline</Tag> : "candidate"}{t.trial === selected && <> <Tag tone="success">selected</Tag></>}</td>
              <td><code>{t.estimator}</code></td>
              <td className="small">{t.folds ? t.folds.map((f) => fmtValue(f)).join(" · ") : "—"}</td>
              <td className="num">{t.mean === null ? "—" : fmtNumber(t.mean, 3)}</td>
              <td>{t.status === "succeeded" ? <span className="small muted">ok</span> : <StatusBadge status={t.status} label={t.error ?? t.status} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small muted">The selection froze on these validation scores before the holdout was read.</p>
    </div>
  );
}

function HoldoutResults({ exp, evaluation }: { exp: MLExperiment; evaluation: Record<string, unknown> | undefined }) {
  const report = (evaluation?.report ?? {}) as { metric?: string; baseline?: Record<string, number>; candidate?: Record<string, number>;
    decision?: { improved?: boolean; gain?: number; min_improvement?: number; reason?: string }; uncertainty?: { ci_low?: number; ci_high?: number; level?: number };
    holdout?: { rows?: number; read_after_selection?: string }; checks?: { check: string; outcome: string; reason: string }[] };
  const metric = report.metric ?? exp.summary.metric ?? "";
  const base = report.baseline?.[metric] ?? exp.summary.baseline?.[metric as never];
  const cand = report.candidate?.[metric] ?? exp.summary.candidate?.[metric as never];
  const d = report.decision ?? exp.summary.decision;
  return (
    <section className="card" aria-label="Holdout results">
      <div className="card-body stack">
        <h3 className="h-sm">Holdout results</h3>
        <KeyValue items={[
          [`Baseline ${metric}`, fmtValue(base)],
          [`Candidate ${metric}`, fmtValue(cand)],
          ["Direction", direction(metric)],
          ["Gain over the baseline", d?.gain == null ? "unknown" : `${fmtValue(d.gain)}${report.uncertainty?.ci_low != null ? ` (95% interval ${fmtValue(report.uncertainty.ci_low)} to ${fmtValue(report.uncertainty.ci_high)})` : ""}`],
          ["Decision", d?.reason ?? (d?.improved ? "improved" : "not improved")],
          ["Holdout rows", fmtValue(report.holdout?.rows)],
        ]} />
        <div className="holdout-consumed" role="note" aria-label="Holdout consumed">
          <strong>Holdout consumed.</strong> It was read once, after the selection <code>{shortHash(report.holdout?.read_after_selection ?? exp.selection_hash, 12)}</code> froze,
          and sealed as <code>{shortHash(exp.evaluation_seal, 12)}</code>. Another spec on split <code>{shortHash(exp.manifest_hash, 12)}</code> is refused: retuning after
          reading a holdout needs a new split.
        </div>
        {!!report.checks?.length && (
          <ul className="small" aria-label="Evaluation checks">
            {report.checks.map((c) => <li key={c.check}><StatusBadge status={c.outcome} /> {c.check.replace(/_/g, " ")}: {c.reason}</li>)}
          </ul>
        )}
      </div>
    </section>
  );
}

/** Other experiments of the same model: grouped by split; scores from different holdouts are never ranked together. */
function OtherExperiments({ exp, all }: { exp: MLExperiment; all: MLExperiment[] }) {
  const others = all.filter((e) => e.id !== exp.id && e.definition_key === exp.definition_key && e.status === "succeeded");
  if (!others.length) return null;
  const metric = exp.summary.metric ?? "";
  return (
    <details className="card">
      <summary>Earlier experiments of {exp.definition_key} ({others.length})</summary>
      <div className="card-body table-wrap" tabIndex={0}>
        <table className="table table-compact">
          <caption className="cap">Holdout {metric} ({direction(metric)}) by split: only rows on the same split are comparable</caption>
          <thead><tr><th scope="col">Experiment</th><th scope="col">Split</th><th scope="col" className="num">Holdout {metric}</th><th scope="col">Comparable here?</th></tr></thead>
          <tbody>
            {others.map((o) => {
              const same = o.manifest_hash === exp.manifest_hash;
              return (
                <tr key={o.id}><td>{o.id} (v{o.definition_version})</td><td><code>{shortHash(o.manifest_hash, 12)}</code></td>
                  <td className="num">{fmtValue(o.summary.candidate?.[metric as never])}</td>
                  <td>{same ? "same split" : <Tag tone="warning">different holdout: not ranked</Tag>}</td></tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function PromotePanel({ wsId, exp, role, onChanged }: { wsId: string; exp: MLExperiment; role: string | undefined; onChanged: () => void }) {
  const mv = exp.model_version;
  const [done, setDone] = useState<string | null>(null);
  if (!mv) {
    return <p className="small muted">{exp.verdict === "improved" ? "No model version was registered." : "Not promotable: only an experiment that beats its baseline registers a model version."}</p>;
  }
  const verified = exp.verification?.state === "ACTIVE" && exp.verification.badge === "verified";
  return (
    <section className="card" aria-label="Promote">
      <div className="card-body stack">
        <h3 className="h-sm">Model version {mv.version} <StatusBadge status={mv.status} /></h3>
        {done ? <Notice tone="success">{done}</Notice> : mv.status === "champion" ? <p className="small">This version is the champion; scoring uses it.</p> : (
          <>
            <p className="small">Promotion makes this version the champion that scoring uses. It needs an approval bound to the package hash and the sealed
              evaluation{!verified ? "; this experiment's verdict is not active, so the server refuses it" : ""}.</p>
            {roleAtLeast(role, "analyst") && (
              <ApprovalStepper wsId={wsId} label={`Request promotion of v${mv.version}`} what={`promoting ${mv.name} v${mv.version}`} disabled={!verified}
                request={() => api.promoteModel(wsId, mv.id)} confirm={(approvalId) => api.promoteModel(wsId, mv.id, approvalId)}
                onDone={(r: ApprovalStep) => { setDone(`Promoted: ${mv.name} v${r.model_version?.version ?? mv.version} is now the champion.`); onChanged(); }} />
            )}
          </>
        )}
      </div>
    </section>
  );
}

export function ExperimentDetail({ wsId, id, role, all, onChanged }: { wsId: string; id: string; role: string | undefined; all: MLExperiment[]; onChanged: () => void }) {
  const exp = useAsync(() => api.experiment(wsId, id), [wsId, id]);
  const has = (r: string) => !!exp.data?.artifacts?.[r];
  const split = useAsync(() => (has("ml_split_manifest") ? api.experimentRecord<{ manifest: SplitManifest; manifest_hash: string }>(wsId, id, "ml_split_manifest")
    : Promise.resolve(undefined)), [wsId, id, exp.data?.artifacts?.ml_split_manifest]);
  const trials = useAsync(() => (has("ml_trials") ? api.experimentRecord<{ trials: MLTrial[]; selection: { trial: number; metric: string } }>(wsId, id, "ml_trials")
    : Promise.resolve(undefined)), [wsId, id, exp.data?.artifacts?.ml_trials]);
  const evaluation = useAsync(() => (has("ml_evaluation") ? api.experimentRecord(wsId, id, "ml_evaluation") : Promise.resolve(undefined)),
    [wsId, id, exp.data?.artifacts?.ml_evaluation]);
  const card = useAsync(() => (has("ml_model_card") ? api.experimentRecord<{ markdown: string; bound: boolean; facts: { label: string; value: unknown }[] }>(wsId, id, "ml_model_card")
    : Promise.resolve(undefined)), [wsId, id, exp.data?.artifacts?.ml_model_card]);
  const dl = useAction();
  if (exp.error) return <ErrorBox error={exp.error} onRetry={exp.reload} />;
  if (!exp.data) return <Loading />;
  const e = exp.data;
  const v = e.verdict ? VERDICT_WORDS[e.verdict] ?? { label: e.verdict, status: e.verdict } : null;
  const metric = trials.data?.content.selection.metric ?? e.summary.metric ?? "";
  return (
    <div className="stack" role="region" aria-label={`Experiment ${e.id}`}>
      <div className="toolbar">
        <h2 className="h-sm">{e.definition_key} v{e.definition_version} · {TASK_WORDS[e.task] ?? e.task}</h2>
        <StatusBadge status={e.status} />{v && <StatusBadge status={v.status} label={v.label} />}
      </div>
      {e.verification && <p className="small">Verification: <VerificationBadge state={e.verification} /></p>}
      {e.error && e.status !== "succeeded" && <Notice tone="danger">{e.error}</Notice>}
      <MlReadiness exp={e} />
      {split.data && (
        <section className="card" aria-label="Split">
          <div className="card-body stack"><h3 className="h-sm">Split</h3><SplitDiagram m={split.data.content.manifest} hash={split.data.content.manifest_hash} /></div>
        </section>
      )}
      {trials.data && (
        <section className="card" aria-label="Experiments table">
          <div className="card-body stack"><h3 className="h-sm">Baseline and candidates</h3>
            <TrialsTable trials={trials.data.content.trials} metric={metric} manifestHash={e.manifest_hash} datasetVersion={e.dataset_version}
              selected={trials.data.content.selection.trial} /></div>
        </section>
      )}
      {e.status === "succeeded" && <HoldoutResults exp={e} evaluation={evaluation.data?.content} />}
      <OtherExperiments exp={e} all={all} />
      {card.data && (
        <section className="card" aria-label="Model card">
          <div className="card-body stack">
            <h3 className="h-sm">Model card</h3>
            <Markdown text={card.data.content.markdown} />
            <p className="small muted">{card.data.content.bound ? "Every number in this card is bound to a recorded fact." : "Some numbers in this card are not bound to recorded facts."}</p>
          </div>
        </section>
      )}
      {e.status === "succeeded" && <PromotePanel wsId={wsId} exp={e} role={role} onChanged={() => { void exp.reload(); onChanged(); }} />}
      <ErrorBox error={dl.error ?? split.error ?? trials.error ?? evaluation.error ?? card.error} />
      <TechnicalDetails value={{ spec_hash: e.spec_hash, manifest_hash: e.manifest_hash, selection_hash: e.selection_hash, evaluation_seal: e.evaluation_seal,
        package_hash: e.package_hash, code_digest: e.code_digest, environment_digest: e.environment_digest, query_ids: e.query_ids }}>
        {e.status === "succeeded" && (
          <button type="button" className="btn btn-xs" disabled={dl.busy}
            onClick={() => void dl.run(async () => saveBlob(await downloadFile(`/workspaces/${encodeURIComponent(wsId)}/ml/experiments/${encodeURIComponent(e.id)}/mlflow`, `${e.id}-mlflow.zip`)))}>
            Download the MLflow export</button>
        )}
      </TechnicalDetails>
    </div>
  );
}

/** Work → Experiments: governed ML experiments, and the ML spec form Predict and Forecast open (P5-03). */
export function ExperimentsPanel({ wsId, role, selected, newKind, onSelect, onNew }: {
  wsId: string; role: string | undefined; selected: string | null; newKind: string | null;
  onSelect: (id: string | null) => void; onNew: (kind: "predict" | "forecast" | null) => void;
}) {
  const exps = useAsync(() => api.experiments(wsId), [wsId]);
  const creating = newKind === "predict" || newKind === "forecast" ? newKind : null;
  const canRun = roleAtLeast(role, "analyst");
  const sorted = useMemo(() => exps.data ?? [], [exps.data]);
  return (
    <div className="split">
      <div className="split-list stack">
        {canRun && !creating && (
          <div className="chip-row">
            <button type="button" className="btn btn-sm btn-primary" onClick={() => onNew("predict")}>New prediction model</button>
            <button type="button" className="btn btn-sm" onClick={() => onNew("forecast")}>New forecast</button>
          </div>
        )}
        <ErrorBox error={exps.error} onRetry={exps.reload} />
        {exps.loading && !exps.data && <Loading />}
        {exps.data?.length === 0 && <EmptyState title="No experiments yet" />}
        {sorted.length > 0 && (
          <ul className="list selectable" aria-label="Experiments">
            {sorted.map((e) => (
              <li key={e.id}>
                <button type="button" className={`list-button ${e.id === selected ? "active" : ""}`} aria-current={e.id === selected ? "true" : undefined}
                  onClick={() => onSelect(e.id)}>
                  <span>{e.definition_key} v{e.definition_version}</span>
                  <span className="small muted">{TASK_WORDS[e.task] ?? e.task} · {e.status}{e.verdict ? ` · ${VERDICT_WORDS[e.verdict]?.label ?? e.verdict}` : ""}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="split-detail">
        {creating ? (
          <MLSpecForm wsId={wsId} kind={creating} onCancel={() => onNew(null)}
            onStarted={(e) => { void exps.reload(); onSelect(e.id); }}
            onRefused={(expId) => { void exps.reload(); onSelect(expId); }} />
        ) : selected ? <ExperimentDetail key={selected} wsId={wsId} id={selected} role={role} all={exps.data ?? []} onChanged={() => void exps.reload()} />
          : <EmptyState title="Select an experiment">Or start a new one: Predict and Forecast in Start work open the spec form here.</EmptyState>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------ Outputs: models and scoring
function ScoreForm({ wsId, champion, onScored }: { wsId: string; champion: ModelVersion; onScored: (r: ScoringRun | null, note: string) => void }) {
  const id = useId();
  const [asset, setAsset] = useState("stg_sn.incident");
  const [output, setOutput] = useState(`${champion.name}_scores`.slice(0, 40));
  const [defId, setDefId] = useState<string | null>(null);
  const prep = useAction();
  const prepare = async (e: FormEvent) => {
    e.preventDefault();
    const d = await prep.run(async () => {
      const draft = await api.createDefinition(wsId, { kind: "ml_scoring", key: `${champion.name}_scoring`, spec: { model: champion.name,
        model_version_id: champion.id, package_hash: champion.package_hash, input: { asset: asset.trim() }, output: output.trim() } });
      return api.publishDefinition(wsId, draft.id, draft.revision);
    });
    if (d) setDefId(d.id);
  };
  return (
    <div className="stack">
      {!defId && (
        <form className="form" onSubmit={prepare} aria-label={`Score with ${champion.name} v${champion.version}`}>
          <div className="form-row">
            <Field label="Input table" htmlFor={`${id}-in`}><input id={`${id}-in`} value={asset} onChange={(e) => setAsset(e.target.value)} /></Field>
            <Field label="Output table" htmlFor={`${id}-out`} hint="Written as ml_<name> in the managed output source, with rejected rows beside it.">
              <input id={`${id}-out`} value={output} onChange={(e) => setOutput(e.target.value)} /></Field>
          </div>
          <p className="small muted">Pinned to v{champion.version} (package <code>{shortHash(champion.package_hash, 10)}</code>); a new version needs a new scoring definition.</p>
          <ErrorBox error={prep.error} />
          <button type="submit" className="btn btn-sm" disabled={prep.busy}>Prepare the scoring definition</button>
        </form>
      )}
      {defId && (
        <ApprovalStepper wsId={wsId} label="Request approval to score" what={`scoring ${asset} with ${champion.name} v${champion.version}`}
          request={() => api.planScoring(wsId, defId)}
          confirm={(approvalId, req) => api.executeScoring(wsId, req.scoring_run_id!, approvalId)}
          onDone={(r) => onScored(r.scoring_run ?? null, r.status === "duplicate" ? "Already scored: this definition ran on the same input; nothing was written twice." : "")} />
      )}
    </div>
  );
}

function ScoringTable({ runs }: { runs: ScoringRun[] }) {
  if (!runs.length) return <p className="small muted">No scoring runs yet.</p>;
  return (
    <div className="table-wrap" tabIndex={0}>
      <table className="table table-compact">
        <caption className="cap">Scoring runs</caption>
        <thead><tr><th scope="col">Run</th><th scope="col">Status</th><th scope="col">Input</th><th scope="col" className="num">Rows in</th>
          <th scope="col" className="num">Scored</th><th scope="col" className="num">Rejected</th><th scope="col">Output</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}><td>{r.id}<div className="muted small">{fmtDate(r.created_at)}</div></td><td><StatusBadge status={r.status} /></td>
              <td><code>{r.input_asset}</code></td><td className="num">{fmtValue(r.rows_input)}</td><td className="num">{fmtValue(r.rows_scored)}</td>
              <td className="num">{r.rows_rejected ? <strong>{fmtValue(r.rows_rejected)}</strong> : fmtValue(r.rows_rejected)}</td>
              <td className="small">{r.output_table ? <code>{r.output_table}</code> : "—"}{r.rejected_table && <div>rejected rows: <code>{r.rejected_table}</code></div>}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Outputs → Models (P5-03): each model's versions (champion, challenger, candidates, retired) with the
 * split each was evaluated on, approved batch scoring pinned to the champion, scoring runs with their
 * rejected rows, and rollback to the previous champion through an approval.
 */
export function ModelsOutput({ wsId, role }: { wsId: string; role: string | undefined }) {
  const models = useAsync(() => api.modelVersions(wsId), [wsId]);
  const exps = useAsync(() => api.experiments(wsId), [wsId]);
  const runs = useAsync(() => api.scoringRuns(wsId), [wsId]);
  const [note, setNote] = useState<string | null>(null);
  const canAct = roleAtLeast(role, "analyst");
  const reload = () => { void models.reload(); void runs.reload(); void exps.reload(); };
  if (models.error) return <ErrorBox error={models.error} onRetry={models.reload} />;
  if (!models.data) return <Loading />;
  const names = [...new Set(models.data.map((m) => m.name))];
  const manifestOf = (m: ModelVersion) => exps.data?.find((e) => e.id === m.experiment_id)?.manifest_hash ?? null;
  return (
    <div className="stack">
      {note && <Notice tone="success">{note}</Notice>}
      {names.length === 0 && <EmptyState title="No models yet">A model version is registered when an experiment beats its baseline.</EmptyState>}
      {names.map((name) => {
        const versions = models.data!.filter((m) => m.name === name).sort((a, b) => b.version - a.version);
        const champion = versions.find((m) => m.status === "champion");
        const challenger = versions.find((m) => m.status === "challenger");
        const metric = versions[0]?.metrics.metric ?? "";
        const comparable = champion && challenger && manifestOf(champion) && manifestOf(champion) === manifestOf(challenger);
        return (
          <section key={name} className="card" aria-label={`Model ${name}`}>
            <div className="card-body stack">
              <h2 className="h-sm">{name}</h2>
              <div className="table-wrap" tabIndex={0}>
                <table className="table table-compact">
                  <caption className="cap">Versions · {metric} ({direction(metric)}) on each version&apos;s own holdout</caption>
                  <thead><tr><th scope="col">Version</th><th scope="col">Status</th><th scope="col" className="num">Holdout {metric}</th>
                    <th scope="col" className="num">Its baseline</th><th scope="col">Split</th><th scope="col">Promoted</th></tr></thead>
                  <tbody>
                    {versions.map((m) => (
                      <tr key={m.id}><td>v{m.version}</td><td><StatusBadge status={m.status} /></td>
                        <td className="num">{fmtValue(m.metrics.candidate?.[metric])}</td><td className="num">{fmtValue(m.metrics.baseline?.[metric])}</td>
                        <td><code>{shortHash(manifestOf(m), 10)}</code></td><td className="small">{m.promoted_at ? fmtDate(m.promoted_at) : "—"}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {champion && challenger && !comparable && (
                <Notice tone="info">Champion v{champion.version} and challenger v{challenger.version} were evaluated on different holdouts, so their scores are not
                  ranked against each other: each is judged against its own baseline. Promote the challenger from its experiment in Work.</Notice>
              )}
              {canAct && champion?.previous_champion_id && (
                <ApprovalStepper wsId={wsId} label="Roll back to the previous champion" what={`rolling ${name} back from v${champion.version}`}
                  request={() => api.rollbackModel(wsId, name)} confirm={(approvalId) => api.rollbackModel(wsId, name, approvalId)}
                  onDone={(r) => { setNote(`Rolled back: ${name} v${r.model_version?.version ?? "?"} is the champion again; v${champion.version} is retired.`); reload(); }} />
              )}
              {canAct && champion && (
                <details className="card">
                  <summary>Score approved data with v{champion.version}</summary>
                  <div className="card-body"><ScoreForm wsId={wsId} champion={champion}
                    onScored={(r, msg) => { setNote(msg || (r ? `Scored ${fmtValue(r.rows_scored)} of ${fmtValue(r.rows_input)} rows into ${r.output_table}; ${fmtValue(r.rows_rejected)} rejected rows are kept in ${r.rejected_table}.` : "Scored.")); reload(); }} /></div>
                </details>
              )}
              {!champion && <p className="small muted">No champion yet: promote a version from its experiment before scoring.</p>}
            </div>
          </section>
        );
      })}
      <ErrorBox error={runs.error} />
      {runs.data && <ScoringTable runs={runs.data} />}
    </div>
  );
}
