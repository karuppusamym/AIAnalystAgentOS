/**
 * Start work job kinds (spec v4 §15): Explain, Compare, Forecast, Predict, Prepare data, Monitor.
 *
 * Availability comes from the capability registry. The API has no job-kind endpoint yet (P4-04
 * builds one), so `jobKindsFromCapabilities` derives each kind from the capabilities it needs; it is
 * the only place that knows the mapping, and the one function to replace when the endpoint lands.
 * A kind that cannot run is returned disabled with its reason, so the UI never starts a pretend run.
 */
import type { CapabilitySummary } from "../api";
import { roleAtLeast } from "../routes";

export type JobKindId = "explain" | "compare" | "forecast" | "predict" | "prepare" | "monitor";

/** What starting the kind does: an investigation, the Prepare data panel, or a new monitor. */
export type JobAction = "investigate" | "prepare" | "monitor";

export interface JobKind {
  id: JobKindId;
  label: string;
  description: string;
  action: JobAction;
  enabled: boolean;
  /** Why it cannot start, in plain words (null when enabled). */
  reason: string | null;
  /** The capabilities the kind would use, for Technical details. */
  uses: string[];
  /** A starting objective for investigation kinds. */
  objectivePrefix?: string;
}

export interface JobContext {
  /** Sources whose tables are selected (status `ready`). */
  readySources: number;
  /** The caller's role in the workspace. */
  role: string | null | undefined;
}

type Need = { what: string; match: (c: CapabilitySummary) => boolean };

const byId = (id: string, what: string): Need => ({ what, match: (c) => c.id === id });
const byTag = (kind: string, tag: string, what: string): Need => ({ what, match: (c) => c.kind === kind && (c.tags ?? []).includes(tag) });
const byKind = (kind: string, what: string): Need => ({ what, match: (c) => c.kind === kind });

interface Spec {
  id: JobKindId;
  label: string;
  description: string;
  action: JobAction;
  needs: Need[];
  minRole: "analyst" | "editor";
  needsData: boolean;
  objectivePrefix?: string;
}

const SPECS: Spec[] = [
  { id: "explain", label: "Explain", description: "Why did a number move? Tests hypotheses and keeps only verified findings.",
    action: "investigate", needs: [byId("playbook.investigate", "the investigation playbook")], minRole: "analyst", needsData: true },
  { id: "compare", label: "Compare", description: "How do groups or periods differ, with a significance test for each difference.",
    action: "investigate", needs: [byId("playbook.investigate", "the investigation playbook"), byTag("Method", "segment", "a segment comparison method")],
    minRole: "analyst", needsData: true, objectivePrefix: "Compare " },
  { id: "forecast", label: "Forecast", description: "Project a metric forward with an interval and a backtest.",
    action: "investigate", needs: [byId("playbook.investigate", "the investigation playbook"), byTag("Method", "forecast", "a forecasting method")],
    minRole: "analyst", needsData: true, objectivePrefix: "Forecast " },
  { id: "predict", label: "Predict", description: "Train and evaluate a model for a labelled target, then score approved data.",
    action: "investigate", needs: [byId("playbook.train", "the model training playbook")], minRole: "analyst", needsData: true },
  { id: "prepare", label: "Prepare data", description: "Load a file or build a recipe: joins, filters and checks, previewed before anything is written.",
    action: "prepare", needs: [byKind("Engine", "a query engine")], minRole: "editor", needsData: false },
  { id: "monitor", label: "Monitor", description: "Watch a metric on a schedule and raise an alert when it breaks a threshold or drifts.",
    action: "monitor", needs: [byTag("Method", "time_series", "a time-series method")], minRole: "analyst", needsData: true },
];

/** Why a capability cannot be used here, or null when it can. */
function blocker(c: CapabilitySummary): string | null {
  if (c.available === false) return c.unavailable_reason || "its extra is not installed";
  if (c.enabled === false) return "it is turned off in this workspace";
  const cert = String(c.certification?.status ?? "");
  if (cert === "draft") return "it is not certified yet";
  if (cert === "deprecated") return "it is deprecated";
  return null;
}

/** The six job kinds, each enabled or disabled with its reason, from the registry and the workspace. */
export function jobKindsFromCapabilities(capabilities: CapabilitySummary[], ctx: JobContext): JobKind[] {
  return SPECS.map((spec) => {
    const uses: string[] = [];
    let reason: string | null = null;
    for (const need of spec.needs) {
      const candidates = capabilities.filter(need.match);
      const usable = candidates.find((c) => blocker(c) === null);
      if (usable) {
        uses.push(usable.id);
        continue;
      }
      if (!reason) {
        reason = candidates.length
          ? `Needs ${need.what}, but ${blocker(candidates[0])}.`
          : `Needs ${need.what}, which is not installed.`;
      }
    }
    if (!reason && !roleAtLeast(ctx.role, spec.minRole)) {
      reason = `Your role (${ctx.role ?? "none"}) cannot start this; it needs ${spec.minRole} or above.`;
    }
    if (!reason && spec.needsData && ctx.readySources === 0) {
      reason = "Connect a source and select its tables first.";
    }
    return { id: spec.id, label: spec.label, description: spec.description, action: spec.action, enabled: reason === null, reason, uses,
      objectivePrefix: spec.objectivePrefix };
  });
}
