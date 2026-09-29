import { useState, type FormEvent } from "react";
import { api, type AskTurn, type Scenario, type ScenarioAdjustment, type ScenarioCell, type SemanticQueryBody } from "../api";
import { fmtNumber } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { ErrorBox, KeyValue, Notice } from "./ui";

/** What-if scenarios (N-9) on a governed Ask answer. The observed baseline is the approved metric query
 * through the gateway; every scenario number is simulated, labelled as such, and never publishable. */

type Kind = ScenarioAdjustment["kind"];
interface AdjustmentDraft { kind: Kind; metric: string; value: string; dimension: string; member: string }
const KIND_LABEL: Record<Kind, string> = { scale: "Change by %", shift: "Add", set: "Set to" };

export function scenarioQuery(turn: AskTurn): SemanticQueryBody | null {
  const p = turn.provenance;
  return p?.governance === "governed" && p.semantic?.query ? p.semantic.query : null;
}

function keyColumns(q: SemanticQueryBody): string[] {
  return [...(q.dimensions ?? []), ...(q.time?.grain ? [q.time.dimension] : [])];
}

/** The request the form describes; null while a number is missing. */
export function buildScenario(name: string, query: SemanticQueryBody, drafts: AdjustmentDraft[], thresholds: Record<string, string>,
  assumptions: string, askTurnId: string | null) {
  const adjustments: ScenarioAdjustment[] = [];
  for (const d of drafts) {
    const n = Number(d.value);
    if (d.value.trim() === "" || !Number.isFinite(n)) return null;
    adjustments.push({ kind: d.kind, metric: d.metric, ...(d.kind === "scale" ? { percent: n } : { amount: n }),
      ...(d.dimension && d.member ? { segment: { dimension: d.dimension, values: [d.member] } } : {}) });
  }
  const filter_overrides = (query.filters ?? []).flatMap((f) => {
    const raw = (thresholds[f.field] ?? "").trim();
    if (!raw) return [];
    const n = Number(raw);
    return [{ field: f.field, value: typeof f.value === "number" && Number.isFinite(n) ? n : raw }];
  });
  if (!adjustments.length && !filter_overrides.length) return null;
  return { name: name.trim() || "What-if scenario", semantic_query: query, adjustments, filter_overrides,
    assumptions: assumptions.split("\n").map((a) => a.trim()).filter(Boolean), ask_turn_id: askTurnId };
}

function Simulated() {
  return <span className="tag tag-warning" title="Computed under the scenario's assumptions, not measured">simulated</span>;
}

function pct(v: number | null) {
  return v === null ? "" : ` (${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%)`;
}

function CellValues({ c }: { c: ScenarioCell }) {
  return (
    <>
      <td>{fmtNumber(c.observed.value)}</td>
      <td className={c.adjusted_by.length ? "strong" : undefined}>{fmtNumber(c.simulated.value)} <Simulated /></td>
      <td>{c.change.value === null ? "—" : `${c.change.value > 0 ? "+" : ""}${fmtNumber(c.change.value)}${pct(c.change_pct.value)}`}</td>
    </>
  );
}

export function ScenarioView({ s }: { s: Scenario }) {
  const keys = s.columns.slice(0, s.columns.length - (s.rows[0]?.cells.length ?? s.totals.length));
  return (
    <section className="stack" aria-label={`Scenario ${s.name}`}>
      <Notice tone="warning"><strong>Simulated, not observed.</strong> These numbers show what the governed result would be under the
        assumptions below. They cannot be published, scheduled or quoted as observed data.</Notice>
      <p>{s.summary}</p>
      <div className="table-wrap">
        <table className="table table-compact">
          <caption className="sr-only">Observed and simulated values</caption>
          <thead>
            <tr>
              {keys.map((k) => <th key={k} scope="col">{k}</th>)}
              <th scope="col">Metric</th><th scope="col">Observed</th><th scope="col">Simulated <Simulated /></th><th scope="col">Change <Simulated /></th>
            </tr>
          </thead>
          <tbody>
            {s.rows.flatMap((r, i) => r.cells.map((c) => (
              <tr key={`${i}-${c.metric}`}>
                {keys.map((k) => <td key={k}>{String(r.key[k] ?? "—")}</td>)}
                <td>{c.metric}</td><CellValues c={c} />
              </tr>
            )))}
            {s.totals.map((c) => (
              <tr key={`total-${c.metric}`} className="strong">
                {keys.map((k, i) => <td key={k}>{i === 0 ? "Total" : ""}</td>)}
                <td>{c.metric}</td><CellValues c={c} />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <h3 className="h-sm">Assumptions</h3>
        <ol className="small" aria-label="Assumptions">{s.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ol>
      </div>
      <KeyValue items={[
        ["Observed baseline", <span key="b">query <code>{s.baseline.query_id}</code>, result <code>{String(s.baseline.result_hash ?? "").slice(0, 12)}</code></span>],
        ["Re-measured", s.remeasured ? <span key="r">query <code>{s.remeasured.query_id}</code> <Simulated /></span> : "—"],
        ["Assumptions hash", <code key="a">{s.assumptions_hash.slice(0, 16)}</code>],
        ["Scenario hash", <code key="h">{s.result_hash.slice(0, 16)}</code>],
      ]} />
    </section>
  );
}

export function WhatIfPanel({ turn }: { turn: AskTurn }) {
  const query = scenarioQuery(turn);
  const [open, setOpen] = useState(false);
  const metrics = query?.metrics ?? [];
  const dims = query ? keyColumns(query) : [];
  const [name, setName] = useState("");
  const [drafts, setDrafts] = useState<AdjustmentDraft[]>([{ kind: "scale", metric: metrics[0] ?? "", value: "", dimension: "", member: "" }]);
  const [thresholds, setThresholds] = useState<Record<string, string>>({});
  const [assumptions, setAssumptions] = useState("");
  const [shown, setShown] = useState<Scenario | null>(null);
  const action = useAction();
  const saved = useAsync(() => (open ? api.scenarios(turn.workspace_id, turn.id) : Promise.resolve([] as Scenario[])), [open, turn.id]);
  if (!query) return null;
  const members = (dim: string) => {
    const i = turn.result?.columns.indexOf(dim) ?? -1;
    return i < 0 ? [] : [...new Set((turn.result?.rows ?? []).map((r) => String(r[i])))].slice(0, 50);
  };
  const body = buildScenario(name, query, drafts, thresholds, assumptions, turn.id);
  const set = (i: number, patch: Partial<AdjustmentDraft>) => setDrafts(drafts.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!body) return;
    const out = await action.run(() => api.runScenario(turn.workspace_id, body));
    if (out) {
      setShown(out);
      saved.reload();
    }
  };
  if (!open) {
    return <div className="btn-row"><button type="button" className="btn btn-sm" onClick={() => setOpen(true)}>What-if scenario</button></div>;
  }
  return (
    <section className="card" aria-label="What-if scenario">
      <div className="card-body stack">
        <header className="btn-row">
          <h3 className="h-sm">What-if scenario</h3>
          <button type="button" className="btn btn-xs btn-ghost" onClick={() => setOpen(false)}>Close</button>
        </header>
        <p className="small muted">The observed numbers come from the approved metric definition through the query gateway. Your changes are applied
          by fixed arithmetic (no model), and every result is labelled simulated.</p>
        <form className="stack" onSubmit={submit}>
          <label>Scenario name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="What if East grows 10%?" /></label>
          {drafts.map((d, i) => (
            <fieldset key={i} className="btn-row" aria-label={`Change ${i + 1}`}>
              <select aria-label="Metric" value={d.metric} onChange={(e) => set(i, { metric: e.target.value })}>
                {metrics.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
              <select aria-label="Change" value={d.kind} onChange={(e) => set(i, { kind: e.target.value as Kind })}>
                {(Object.keys(KIND_LABEL) as Kind[]).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
              </select>
              <input aria-label="Value" inputMode="decimal" value={d.value} onChange={(e) => set(i, { value: e.target.value })}
                placeholder={d.kind === "scale" ? "10 or -5" : "amount"} />
              {dims.length > 0 && (
                <select aria-label="Only where" value={d.dimension} onChange={(e) => set(i, { dimension: e.target.value, member: "" })}>
                  <option value="">on every row</option>
                  {dims.map((x) => <option key={x} value={x}>where {x} is…</option>)}
                </select>
              )}
              {d.dimension && (
                <select aria-label="Member" value={d.member} onChange={(e) => set(i, { member: e.target.value })}>
                  <option value="">choose</option>
                  {members(d.dimension).map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              )}
              {drafts.length > 1 && <button type="button" className="btn btn-xs btn-ghost" onClick={() => setDrafts(drafts.filter((_, j) => j !== i))}>Remove</button>}
            </fieldset>
          ))}
          {drafts.length < 10 && <button type="button" className="btn btn-xs" onClick={() => setDrafts([...drafts,
            { kind: "scale", metric: metrics[0] ?? "", value: "", dimension: "", member: "" }])}>Add a change</button>}
          {(query.filters ?? []).length > 0 && (
            <div className="stack">
              <p className="small">Change a threshold (the query is re-measured with the new value):</p>
              {(query.filters ?? []).map((f) => (
                <label key={f.field} className="small">{f.field} {f.op ?? "="} {String(f.value ?? "")} → new value
                  <input value={thresholds[f.field] ?? ""} onChange={(e) => setThresholds({ ...thresholds, [f.field]: e.target.value })} />
                </label>
              ))}
            </div>
          )}
          <label>Assumptions (one per line)<textarea rows={2} value={assumptions} onChange={(e) => setAssumptions(e.target.value)} /></label>
          <button className="btn btn-primary btn-sm" disabled={action.busy || !body}>{action.busy ? "Computing…" : "Run scenario"}</button>
        </form>
        <ErrorBox error={action.error} />
        {shown && <ScenarioView s={shown} />}
        {(saved.data ?? []).length > 0 && (
          <div>
            <h3 className="h-sm">Scenarios on this answer</h3>
            <ul className="list compact" aria-label="Saved scenarios">{(saved.data ?? []).map((s) => (
              <li key={s.id} className="list-item small">
                <span>{s.name} <Simulated /></span>
                <button type="button" className="btn btn-xs btn-ghost" onClick={() => setShown(s)}>Show</button>
              </li>
            ))}</ul>
          </div>
        )}
      </div>
    </section>
  );
}
