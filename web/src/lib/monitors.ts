/** Monitor form helpers and chart overlays (services/monitors.py semantics). Pure functions. */
import type { Alert, Monitor, MonitorConfig, MonitorKind } from "../api";
import { driftBaseline, type MonitorOverlay } from "./charts";

export const MONITOR_KINDS: { id: MonitorKind; label: string; description: string }[] = [
  { id: "metric_threshold", label: "Threshold", description: "Alert when the latest period crosses a fixed bound." },
  { id: "metric_drift", label: "Drift", description: "Alert when the latest period is far from the recent median (robust z-score)." },
  { id: "change_point", label: "Change point", description: "Alert on a statistically significant regime shift in the recent periods." },
  { id: "forecast_deviation", label: "Forecast deviation", description: "Alert when the latest period falls outside the forecast interval fitted on the prior periods (trend and seasonality aware)." },
  { id: "data_quality", label: "Data quality", description: "Alert on new or worsening data-quality issues versus the recorded baseline." },
];

export const GRAINS = ["day", "week", "month"] as const;
export const OPS = [">", ">=", "<", "<="] as const;

export interface MonitorFormState {
  name: string;
  kind: MonitorKind;
  metric: string;
  grain: (typeof GRAINS)[number];
  op: (typeof OPS)[number];
  value: string;
  severity: "warning" | "critical" | "info";
  lookback: string;
  zThreshold: string;
  recentPeriods: string;
  forecastZ: string;
  history: string;
  seasonalPeriods: string;
  assets: string[];
  autoInvestigate: boolean;
}

export function emptyMonitorForm(): MonitorFormState {
  return {
    name: "", kind: "metric_drift", metric: "", grain: "week", op: ">", value: "", severity: "warning",
    lookback: "8", zThreshold: "3", recentPeriods: "4", forecastZ: "2.5", history: "60", seasonalPeriods: "",
    assets: [], autoInvestigate: false,
  };
}

const num = (s: string) => (s.trim() === "" ? NaN : Number(s));

export function validateMonitorForm(f: MonitorFormState): Record<string, string> {
  const e: Record<string, string> = {};
  if (!f.name.trim()) e.name = "Name is required.";
  if (f.kind !== "data_quality" && !f.metric) e.metric = "Choose a metric.";
  if (f.kind === "metric_threshold" && !Number.isFinite(num(f.value))) e.value = "Threshold value must be a number.";
  if (f.kind === "metric_drift") {
    if (!(Number.isInteger(num(f.lookback)) && num(f.lookback) >= 4)) e.lookback = "Lookback must be a whole number ≥ 4.";
    if (!(num(f.zThreshold) > 0)) e.zThreshold = "z threshold must be positive.";
  }
  if (f.kind === "change_point" && !(Number.isInteger(num(f.recentPeriods)) && num(f.recentPeriods) >= 1)) {
    e.recentPeriods = "Recent periods must be a whole number ≥ 1.";
  }
  if (f.kind === "forecast_deviation") {
    if (!(num(f.forecastZ) > 0)) e.forecastZ = "z must be positive.";
    if (!(Number.isInteger(num(f.history)) && num(f.history) >= 4)) e.history = "History must be a whole number ≥ 4 (3 to fit, 1 to check).";
    if (f.seasonalPeriods.trim() && !(Number.isInteger(num(f.seasonalPeriods)) && num(f.seasonalPeriods) >= 2)) {
      e.seasonalPeriods = "Season length must be a whole number ≥ 2, or empty to infer it from the grain.";
    }
  }
  return e;
}

export function buildMonitorConfig(f: MonitorFormState): MonitorConfig {
  switch (f.kind) {
    case "metric_threshold":
      return { metric: f.metric, grain: f.grain, op: f.op, value: num(f.value), severity: f.severity };
    case "metric_drift":
      return { metric: f.metric, grain: f.grain, lookback: num(f.lookback), z_threshold: num(f.zThreshold) };
    case "change_point":
      return { metric: f.metric, grain: f.grain, recent_periods: num(f.recentPeriods) };
    case "forecast_deviation": {
      const cfg: MonitorConfig = { metric: f.metric, grain: f.grain, z: num(f.forecastZ), history: num(f.history) };
      if (f.seasonalPeriods.trim()) cfg.seasonal_periods = num(f.seasonalPeriods);
      return cfg;
    }
    case "data_quality":
      return f.assets.length ? { assets: [...f.assets] } : {};
    default:
      return {};
  }
}

/** Reference lines for a monitor's chart: the drift baseline median or the threshold. */
export function monitorOverlay(m: Monitor, points: [string, number][]): MonitorOverlay {
  const alerting = m.state === "alerting";
  if (m.kind === "metric_drift") {
    // The evaluator's baseline only applies when it evaluated the same latest period the chart shows.
    const fromResult = m.last_result?.baseline_median;
    const samePeriod = points.length > 0 && m.last_result?.period === points[points.length - 1][0];
    return {
      alerting,
      baselineMedian: typeof fromResult === "number" && samePeriod ? fromResult : driftBaseline(points, Number(m.config.lookback ?? 8)),
    };
  }
  if (m.kind === "metric_threshold" && typeof m.config.value === "number" && m.config.op) {
    return { alerting, threshold: { op: m.config.op, value: m.config.value } };
  }
  return { alerting };
}

export function monitorMessage(m: Monitor): string {
  const r = m.last_result ?? {};
  return (r.error ? `Error: ${r.error}` : r.message ?? r.reason ?? "") || (m.last_evaluated_at ? "" : "Not evaluated yet.");
}

export interface TriageExplanation {
  ruleSeverity: string | null;
  /** JEV's materiality probability, or null when triage did not run. */
  pMaterial: number | null;
  model: string | null;
  escalated: boolean;
  /** Plain-language account; JEV is described as escalate-only because it is (services/monitors.py). */
  lines: string[];
}

/** Why an alert has its severity: the monitor rule first, then JEV, which may only escalate. */
export function explainTriage(a: Alert): TriageExplanation {
  const d = (a.data ?? {}) as Record<string, unknown>;
  const ruleSeverity = typeof d.severity === "string" ? d.severity : null;
  const triage = a.data?.triage ?? null;
  const p = typeof triage?.p_material === "number" ? triage.p_material : null;
  const escalated = ruleSeverity !== null && ruleSeverity !== a.severity;
  const rule = a.message || String(d.message ?? "") || "the monitor rule fired";
  const lines = [`Rule: ${rule}${ruleSeverity ? ` (rule severity: ${ruleSeverity})` : ""}.`];
  if (p === null) {
    lines.push("JEV triage did not run; the severity is the rule's.");
  } else if (escalated) {
    lines.push(`JEV judged it material (p ${Math.round(p * 100)}%) and escalated the severity from ${ruleSeverity} to ${a.severity}.`);
  } else {
    lines.push(`JEV materiality p ${Math.round(p * 100)}%: the severity stays as the rule set it.`);
  }
  lines.push("JEV can only raise severity; it never lowers it or closes an alert.");
  return { ruleSeverity, pMaterial: p, model: triage?.model ?? null, escalated, lines };
}

export function describeMonitorConfig(m: Monitor): string {
  const c = m.config ?? {};
  switch (m.kind) {
    case "metric_threshold":
      return `${c.metric ?? "?"} per ${c.grain ?? "week"} ${c.op ?? ""} ${c.value ?? ""}`;
    case "metric_drift":
      return `${c.metric ?? "?"} per ${c.grain ?? "week"} · |z| ≥ ${c.z_threshold ?? 3} vs ${c.lookback ?? 8}-period median`;
    case "change_point":
      return `${c.metric ?? "?"} per ${c.grain ?? "week"} · shift in last ${c.recent_periods ?? 4} periods`;
    case "forecast_deviation":
      return `${c.metric ?? "?"} per ${c.grain ?? "week"} · outside the ±${c.z ?? 2.5}σ forecast from ${c.history ?? 60} periods`
        + (c.seasonal_periods ? ` · season ${c.seasonal_periods}` : "");
    case "data_quality":
      return c.assets?.length ? `assets: ${c.assets.join(", ")}` : "all assets in scope";
    default:
      return "";
  }
}
