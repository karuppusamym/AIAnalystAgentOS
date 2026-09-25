import type { ComponentType } from "react";
import { guessChart, type Preview } from "../lib/charts";
import { fmtNumber, fmtP } from "../lib/format";
import { ChartView } from "./Chart";
import { DataTable, EmptyState, KeyValue, RecordTable, StatusBadge, TechnicalDetails, Value } from "./ui";

/**
 * Result renderers keyed by a manifest's `ui.renderer` (spec v3 §9). A capability names the
 * renderer for its output; the registry maps the id to a component, and anything it does not
 * know falls back to `renderer.json`: primitive fields as key/value pairs, the raw document only
 * under "Technical details". A new method or tool therefore needs no new screen, only a manifest.
 */
export interface RendererProps {
  value: unknown;
  title?: string;
}

export type Renderer = ComponentType<RendererProps>;

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** Tabular shape of a result, if it has one: {columns, rows} | {preview} | [{…}] | {rows|records|data: [{…}]}. */
export function asTable(v: unknown): { columns: string[]; rows: unknown[][] } | null {
  if (Array.isArray(v)) {
    if (v.length && v.every(isObj)) {
      const cols = [...new Set(v.flatMap((r) => Object.keys(r as object)))];
      return { columns: cols, rows: v.map((r) => cols.map((c) => (r as Record<string, unknown>)[c])) };
    }
    return null;
  }
  if (!isObj(v)) return null;
  if (isObj(v.preview)) return asTable(v.preview);
  if (Array.isArray(v.columns) && Array.isArray(v.rows)) {
    const columns = v.columns.map(String);
    const rows = (v.rows as unknown[]).map((r) => (Array.isArray(r) ? r : isObj(r) ? columns.map((c) => r[c]) : [r]));
    return { columns, rows };
  }
  for (const key of ["rows", "records", "data", "items", "result"]) if (Array.isArray(v[key])) return asTable(v[key]);
  return null;
}

function isStatResult(v: unknown): v is Record<string, unknown> {
  return isObj(v) && ("p_value" in v || "p_adjusted" in v || "effect_size" in v) && ("test" in v || "n" in v);
}

function primitives(v: Record<string, unknown>): [string, unknown][] {
  return Object.entries(v).filter(([, x]) => x === null || ["string", "number", "boolean"].includes(typeof x));
}

export function JsonRenderer({ value }: RendererProps) {
  const prims = isObj(value) ? primitives(value) : [];
  return (
    <div className="renderer renderer-json">
      {prims.length > 0 ? (
        <KeyValue items={prims.map(([k, x]) => [k.replace(/_/g, " "), typeof x === "number" ? <Value value={x} /> : x === null ? "—" : String(x)])} />
      ) : typeof value === "string" ? <p>{value}</p> : typeof value === "number" ? <p><Value value={value} /></p> : (
        <p className="muted small">This result has no summary view; its full content is under Technical details.</p>
      )}
      <TechnicalDetails value={value} />
    </div>
  );
}

export function TableRenderer({ value, title }: RendererProps) {
  const t = asTable(value);
  if (!t) return <JsonRenderer value={value} />;
  return (
    <div className="renderer renderer-table">
      <DataTable columns={t.columns} rows={t.rows} maxRows={200} caption={title ?? "Result"} />
      <TechnicalDetails value={value} />
    </div>
  );
}

export function ChartRenderer({ value, title }: RendererProps) {
  const t = asTable(value);
  if (!t) return <JsonRenderer value={value} />;
  const spec = isObj(value) && isObj(value.chart) ? value.chart : isObj(value) ? value : {};
  const hint = typeof spec.type === "string" ? spec.type : null;
  const type = guessChart(t.columns, t.rows, hint) ?? "table";
  const preview: Preview = { columns: t.columns, rows: t.rows };
  return (
    <div className="renderer renderer-chart">
      <ChartView type={type} preview={preview} title={title} />
      <TechnicalDetails value={value} />
    </div>
  );
}

export function StatResultRenderer({ value }: RendererProps) {
  if (!isStatResult(value)) return <JsonRenderer value={value} />;
  const v = value;
  const groups = Array.isArray(v.groups) ? (v.groups as Record<string, unknown>[]).filter(isObj) : [];
  const warnings = Array.isArray(v.warnings) ? v.warnings.map(String) : [];
  const supported = typeof v.supported === "boolean" ? v.supported : null;
  return (
    <div className="renderer renderer-stat">
      <div className="chip-row">
        {supported !== null && <StatusBadge status={supported ? "supported" : "rejected"} label={supported ? "supported" : "not supported"} />}
        {typeof v.test === "string" && <span className="small"><span className="muted">test</span> <code>{v.test}</code></span>}
      </div>
      <KeyValue items={[
        ["n", <Value key="n" value={v.n} format="int" />],
        ["p-value", v.p_value === undefined || v.p_value === null ? <Value key="p" value={null} /> : fmtP(v.p_value)],
        ["q-value (BH-adjusted)", v.p_adjusted === undefined || v.p_adjusted === null ? <Value key="q" value={null} /> : fmtP(v.p_adjusted)],
        [String(v.effect_label ?? "effect size"), v.effect_size === undefined || v.effect_size === null ? <Value key="e" value={null} /> : fmtNumber(v.effect_size, 3)],
      ]} />
      {warnings.length > 0 && <p className="small warn-text">⚠ {warnings.join("; ")}</p>}
      {groups.length > 0 && <RecordTable records={groups} maxRows={30} />}
      <TechnicalDetails value={value} />
    </div>
  );
}

const REGISTRY = new Map<string, Renderer>([
  ["renderer.json", JsonRenderer],
  ["renderer.table", TableRenderer],
  ["renderer.chart", ChartRenderer],
  ["renderer.stat_result", StatResultRenderer],
]);

/** Add a renderer (a plugin bundle or a later row, e.g. renderer.finding / renderer.diff). */
export function registerRenderer(id: string, component: Renderer): void {
  REGISTRY.set(id, component);
}

export function knownRenderers(): string[] {
  return [...REGISTRY.keys()];
}

/**
 * The renderer for a result: the manifest's choice when registered, otherwise one inferred from
 * the result's shape, otherwise JSON. Returns the id actually used so the UI can say so.
 */
export function resolveRenderer(id: string | null | undefined, value: unknown): { id: string; component: Renderer } {
  if (id && REGISTRY.has(id)) return { id, component: REGISTRY.get(id)! };
  const inferred = isStatResult(value) ? "renderer.stat_result" : asTable(value) ? "renderer.table" : "renderer.json";
  return { id: inferred, component: REGISTRY.get(inferred)! };
}

export function ResultView({ rendererId, value, title }: { rendererId?: string | null; value: unknown; title?: string }) {
  if (value === undefined) return <EmptyState title="No result" />;
  const r = resolveRenderer(rendererId, value);
  const C = r.component;
  return (
    <div className="result-view" data-renderer={r.id}>
      {rendererId && rendererId !== r.id && (
        <p className="muted small">Renderer <code>{rendererId}</code> is not available in this UI; showing <code>{r.id}</code>.</p>
      )}
      <C value={value} title={title} />
    </div>
  );
}
