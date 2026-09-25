import { Link } from "react-router-dom";
import type { ChangedFinding, MetricDelta, RunChanges } from "../api";
import { fmtNumber, fmtPct } from "../lib/format";
import { Card, Tag } from "./ui";

const GROUPS: { key: "new" | "changed" | "persisting" | "resolved"; label: string; tone: string; hint: string }[] = [
  { key: "new", label: "New", tone: "info", hint: "verified now, not in the previous run" },
  { key: "changed", label: "Changed", tone: "warning", hint: "same claim, effect moved ≥ 25%" },
  { key: "persisting", label: "Persisting", tone: "neutral", hint: "same claim, similar effect" },
  { key: "resolved", label: "Resolved", tone: "success", hint: "in the previous run, not verified now" },
];

/** ▲ / ▼ / ▬ for a relative change; null when there is nothing to compare. */
export function deltaArrow(pct: number | null | undefined): { arrow: string; dir: "up" | "down" | "flat" } | null {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return null;
  if (Math.abs(pct) < 0.0005) return { arrow: "▬", dir: "flat" };
  return pct > 0 ? { arrow: "▲", dir: "up" } : { arrow: "▼", dir: "down" };
}

function FindingItem({ f, wsId, showPrev }: { f: ChangedFinding; wsId: string; showPrev: boolean }) {
  return (
    <li className="change-item">
      {f.id ? <Link to={`/w/${wsId}/insights/${f.id}`}><strong>{f.code ? `${f.code} ` : ""}{f.title ?? "Finding"}</strong></Link>
        : <strong>{f.code ? `${f.code} ` : ""}{f.title ?? "Finding"}</strong>}
      {f.finding && <div className="small muted clamp-2">{f.finding}</div>}
      {showPrev && (f.effect !== undefined || f.previous_effect !== undefined) && (
        <div className="small">effect {fmtNumber(f.previous_effect ?? null, 3)} → <strong>{fmtNumber(f.effect ?? null, 3)}</strong></div>
      )}
    </li>
  );
}

function MetricRow({ m }: { m: MetricDelta }) {
  const d = deltaArrow(m.pct_change);
  return (
    <tr>
      <th scope="row">{m.name}</th>
      <td className="num">{fmtNumber(m.previous_value)}</td>
      <td className="num">{fmtNumber(m.value)}</td>
      <td className="num">
        {d ? (
          <span className={`delta delta-${d.dir}`} aria-label={`${d.dir === "up" ? "up" : d.dir === "down" ? "down" : "unchanged"} ${fmtPct(m.pct_change)}`}>
            <span aria-hidden="true">{d.arrow}</span> {fmtPct(m.pct_change)}
          </span>
        ) : <span className="muted">—</span>}
      </td>
    </tr>
  );
}

/** "What changed since the previous run" for recurring analyses (run.summary.changes). */
export function ChangesPanel({ changes, wsId, reportArtifactId }: { changes: RunChanges; wsId: string; reportArtifactId?: string | null }) {
  const metrics = changes.metrics ?? [];
  return (
    <Card title="What changed since the previous run"
      actions={<>
        {changes.previous_run_id && <Link className="btn btn-xs btn-ghost" to={`/w/${wsId}/runs/${changes.previous_run_id}`}>Previous run</Link>}
        {reportArtifactId && <Link className="btn btn-xs" to={`/w/${wsId}/reports?artifact=${reportArtifactId}`}>Generated report</Link>}
      </>}>
      <div className="chip-row changes-counts" aria-label="Finding changes">
        {GROUPS.map((g) => <Tag key={g.key} tone={g.tone}>{g.label}: {(changes[g.key] ?? []).length}</Tag>)}
      </div>
      <div className="grid-2 changes-grid">
        {GROUPS.map((g) => {
          const items = changes[g.key] ?? [];
          return (
            <section key={g.key} className="changes-group" aria-label={`${g.label} findings`}>
              <h3 className="group-title">{g.label} <span className="muted small">({items.length}) · {g.hint}</span></h3>
              {items.length === 0 ? <p className="muted small">None.</p> : (
                <ul className="change-list">{items.map((f, i) => <FindingItem key={f.id ?? i} f={f} wsId={wsId} showPrev={g.key === "changed"} />)}</ul>
              )}
            </section>
          );
        })}
      </div>
      {metrics.length > 0 && (
        <div className="table-wrap">
          <table className="table table-compact">
            <caption className="sr-only">KPI changes</caption>
            <thead><tr><th scope="col">KPI</th><th scope="col" className="num">Previous</th><th scope="col" className="num">Now</th><th scope="col" className="num">Change</th></tr></thead>
            <tbody>{metrics.map((m) => <MetricRow key={m.name} m={m} />)}</tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
