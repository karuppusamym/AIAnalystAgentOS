/**
 * Start work job kinds (spec v4 §15): Explain, Compare, Forecast, Predict, Prepare data, Monitor.
 *
 * Availability is decided by the server (P4-04, `GET /api/workspaces/{ws}/capabilities`, from
 * `capabilities/job_kinds.py`): an executor, usable capabilities, the caller's role and data in scope.
 * This module only adds the plain-language description and what starting a kind does in the UI; it
 * never re-derives availability, so a kind the server says cannot run is shown disabled with every
 * reason and its remediation, and is never started.
 */
import type { JobKindAvailability, JobKindKey, JobKindReason } from "../api";

export type JobKindId = JobKindKey;

/** What starting the kind does: an investigation, the ML spec form, the Prepare data panel, or a new monitor. */
export type JobAction = "investigate" | "ml" | "prepare" | "monitor";

export interface JobKind {
  id: JobKindId;
  label: string;
  description: string;
  action: JobAction;
  enabled: boolean;
  /**
   * An ML kind whose only missing piece is a published spec: it cannot train yet, but Start work opens
   * its form so the person can write the spec (the remedy) instead of meeting a dead end.
   */
  specFirst: boolean;
  /** Every reason it cannot start, each with its remediation (empty when enabled). */
  reasons: JobKindReason[];
  /** The first reason in plain words (null when enabled). */
  reason: string | null;
  /** The capabilities the kind would use, for Technical details. */
  uses: string[];
  /** The readiness checks that decide whether it can start on chosen data. */
  readinessChecks: string[];
  minRole: string;
  /** A starting objective for investigation kinds. */
  objectivePrefix?: string;
}

const DESCRIPTIONS: Record<JobKindId, { description: string; objectivePrefix?: string }> = {
  explain: { description: "Why did a number move? Tests hypotheses and keeps only verified findings." },
  compare: { description: "How do groups or periods differ, with a significance test for each difference.", objectivePrefix: "Compare " },
  forecast: { description: "Project a metric forward with an interval, backtested against a seasonal-naive baseline." },
  predict: { description: "Train and evaluate a model for a labelled target against a baseline, then score approved data." },
  prepare: { description: "Load a file, build a recipe or a pipeline: joins, filters and checks, dry-run before anything is written." },
  monitor: { description: "Watch a metric or a model on a schedule and raise an alert when it breaks a threshold or drifts." },
};

export function actionOf(a: Pick<JobKindAvailability, "entry">): JobAction {
  if (a.entry.type === "recipe") return "prepare";
  if (a.entry.type === "monitor") return "monitor";
  if (a.entry.payload_type === "ml") return "ml";
  return "investigate";
}

/** The server's job kinds as UI choices, in the server's order. */
export function jobKindsFromAvailability(list: JobKindAvailability[]): JobKind[] {
  return list.map((a) => {
    const copy = DESCRIPTIONS[a.key] ?? { description: "" };
    const reasons = a.available ? [] : (a.reasons.length ? a.reasons : [{ code: "unknown", message: "The server did not say why.", remediation: "" }]);
    const action = actionOf(a);
    return {
      id: a.key, label: a.label, description: copy.description, action, enabled: a.available,
      specFirst: !a.available && action === "ml" && reasons.every((r) => r.code === "no_ml_spec"), reasons,
      reason: reasons[0]?.message ?? null, uses: a.capabilities.filter((c) => c.usable).map((c) => c.id),
      readinessChecks: a.readiness_checks, minRole: a.min_role, objectivePrefix: copy.objectivePrefix,
    };
  });
}

/** Plain words for a readiness check id (contracts/brief.py CHECKS). */
export const CHECK_WORDS: Record<string, string> = {
  capability: "Something can run it here", scope: "The data is in your scope", freshness: "The data is fresh enough",
  schema_drift: "The columns have not changed", grain: "One row means one thing (grain)", key_uniqueness: "Keys are unique",
  join_fanout: "Joins do not duplicate rows", coverage: "Enough history is covered", missingness: "Few values are missing",
  label_availability: "Labels exist and are known in time",
};

export const READINESS_WORDS: Record<string, { label: string; status: string }> = {
  ready: { label: "Ready", status: "ok" },
  needs_input: { label: "Needs your input", status: "pending" },
  blocked: { label: "Blocked", status: "failed" },
  unsupported: { label: "Not supported here", status: "refused" },
};
