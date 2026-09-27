import type { ReactNode } from "react";
import type { MeasuredProfile, ProfileMeta } from "../api";
import { fmtNumber, fmtPct } from "../lib/format";

/**
 * Column profiles as measured by the crawler through the gateway (skills/profiling.py): completeness,
 * distinct values, range, most common values, a histogram, monthly volume and value formats. Nothing
 * here is computed from raw rows in the browser; it only presents what was measured. Sensitive columns
 * arrive without values, ranges or top values (the server strips them), and the view says so.
 */
export function hasProfile(p: MeasuredProfile | null | undefined): p is MeasuredProfile {
  return !!p && (p.null_rate !== undefined || p.distinct !== undefined || !!p.top_values?.length);
}

function short(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return fmtNumber(v);
  const s = String(v);
  if (/^\d{4}-\d{2}-\d{2}T/.test(s)) return s.slice(0, 10);
  return s.length > 28 ? `${s.slice(0, 27)}…` : s;
}

/** "from 1 to 5", "2023-01-02 → 2026-09-25", or null when no range was measured (or it was withheld). */
export function rangeText(p: MeasuredProfile): string | null {
  if (p.min === undefined || p.min === null || p.max === undefined || p.max === null) return null;
  if (String(p.min) === String(p.max)) return `always ${short(p.min)}`;
  return `${short(p.min)} → ${short(p.max)}`;
}

/** One line a person can read at a glance: completeness, cardinality, then the most telling fact. */
export function profileLine(p: MeasuredProfile, rowCount?: number | null): string {
  const parts: string[] = [];
  const nullRate = p.null_rate;
  if (nullRate !== undefined) parts.push(nullRate === 0 ? "always filled" : nullRate >= 0.999 ? "always empty" : `${fmtPct(nullRate, nullRate < 0.01 ? 1 : 0)} empty`);
  if (p.distinct !== undefined) {
    const unique = rowCount != null && rowCount > 0 && p.distinct === rowCount - Math.round((p.null_rate ?? 0) * rowCount);
    parts.push(unique && p.distinct > 1 ? "unique per row" : `${fmtNumber(p.distinct)} distinct`);
  }
  if (p.values_complete && p.values?.length) parts.push(`one of ${p.values.slice(0, 4).map(short).join(", ")}${p.values.length > 4 ? "…" : ""}`);
  else {
    const r = rangeText(p);
    if (r) parts.push(r);
  }
  return parts.join(" · ");
}

export function CompletenessBar({ nullRate }: { nullRate: number | undefined }) {
  const filled = Math.max(0, Math.min(1, 1 - (nullRate ?? 0)));
  const tone = filled >= 0.98 ? "good" : filled >= 0.8 ? "fair" : "poor";
  return (
    <span className={`completeness completeness-${tone}`} title={`${fmtPct(filled, 1)} of rows have a value`}>
      <span className="completeness-track" aria-hidden="true"><span className="completeness-fill" style={{ width: `${filled * 100}%` }} /></span>
      <span className="completeness-label">{fmtPct(filled, filled > 0.99 && filled < 1 ? 1 : 0)}</span>
    </span>
  );
}

function Bars({ items, label }: { items: { label: string; value: number; share: number }[]; label: string }) {
  if (!items.length) return null;
  const max = Math.max(...items.map((i) => i.share), 0.0001);
  return (
    <table className="bars" aria-label={label}>
      <tbody>
        {items.map((i) => (
          <tr key={i.label}>
            <th scope="row" className="bars-label" title={i.label}>{i.label}</th>
            <td className="bars-cell"><span className="bars-bar" style={{ width: `${(i.share / max) * 100}%` }} aria-hidden="true" /></td>
            <td className="bars-value num">{fmtPct(i.share, i.share < 0.01 ? 1 : 0)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Column bars for a histogram or monthly volume; heights only, the numbers are in the title and the caption. */
function Columns({ points, label, caption }: { points: { key: string; value: number; title: string }[]; label: string; caption: ReactNode }) {
  if (points.length < 2) return null;
  const max = Math.max(...points.map((p) => p.value), 1);
  return (
    <figure className="spark" aria-label={label}>
      <div className="spark-cols" role="img" aria-label={`${label}: ${points.length} bars`}>
        {points.map((p) => (
          <span key={p.key} className="spark-col" title={p.title} style={{ height: `${Math.max(2, (p.value / max) * 100)}%` }} />
        ))}
      </div>
      <figcaption className="spark-caption">{caption}</figcaption>
    </figure>
  );
}

/** Everything measured about one column, for its expanded row in the catalog. */
export function ColumnProfilePanel({ profile, sensitive, rowCount }: { profile: MeasuredProfile; sensitive: boolean; rowCount?: number | null }) {
  const p = profile;
  const facts: [string, ReactNode][] = [];
  facts.push(["Filled", <CompletenessBar key="c" nullRate={p.null_rate} />]);
  if (p.distinct !== undefined) facts.push(["Distinct values", fmtNumber(p.distinct)]);
  const range = sensitive ? null : rangeText(p);
  if (range) facts.push(["Range", range]);
  if (!sensitive) {
    if (p.mean != null) facts.push(["Average", fmtNumber(p.mean)]);
    if (p.percentiles?.p50 != null) facts.push(["Median", fmtNumber(p.percentiles.p50)]);
    if (p.stddev != null) facts.push(["Spread (std. dev.)", fmtNumber(p.stddev)]);
    if (p.true_count != null && p.non_null) facts.push(["True", fmtPct(p.true_count / p.non_null, 0)]);
    if (p.avg_length != null) facts.push(["Text length", `${fmtNumber(p.avg_length)} on average, up to ${fmtNumber(p.max_length ?? null)}`]);
    if (p.has_blanks) facts.push(["Blanks", "Some rows are empty or blank"]);
    const outliers = (p.outliers?.low_count ?? 0) + (p.outliers?.high_count ?? 0);
    if (outliers > 0) facts.push(["Unusual values", `${fmtNumber(outliers)} outside the typical range`]);
  }

  const top = (p.top_values ?? []).map((t) => ({ label: short(t.value), value: t.count, share: t.share ?? (p.non_null ? t.count / p.non_null : 0) }));
  const hist = (p.histogram ?? []).map((b) => ({ key: String(b.bin), value: b.count, title: `${short(b.low)} – ${short(b.high)}: ${fmtNumber(b.count)}` }));
  const months = (p.monthly_counts ?? []).map((m) => ({ key: m.month, value: m.count, title: `${short(m.month).slice(0, 7)}: ${fmtNumber(m.count)}` }));
  const patterns = p.patterns ?? [];

  return (
    <div className="profile-panel">
      <dl className="profile-facts">
        {facts.map(([k, v]) => <div key={k} className="profile-fact"><dt>{k}</dt><dd>{v}</dd></div>)}
      </dl>
      {sensitive && <p className="muted small">Values, ranges and most common values are withheld for this sensitive column.</p>}
      <div className="profile-charts">
        {!sensitive && top.length > 0 && (
          <div>
            <h4 className="profile-h">{p.values_complete ? "All values" : "Most common values"}</h4>
            <Bars items={top} label="Most common values" />
          </div>
        )}
        {!sensitive && hist.length > 1 && (
          <div>
            <h4 className="profile-h">Distribution</h4>
            <Columns points={hist} label="Distribution" caption={<>{short(p.histogram![0].low)} to {short(p.histogram![p.histogram!.length - 1].high)}</>} />
          </div>
        )}
        {!sensitive && months.length > 1 && (
          <div>
            <h4 className="profile-h">Rows per month</h4>
            <Columns points={months} label="Rows per month"
              caption={<>{short(months[0].key).slice(0, 7)} to {short(months[months.length - 1].key).slice(0, 7)}</>} />
          </div>
        )}
        {!sensitive && patterns.length > 0 && (
          <div>
            <h4 className="profile-h">Value formats</h4>
            <ul className="pattern-list">
              {patterns.map((pt) => (
                <li key={pt.mask}><code title="A = letter, 9 = digit">{pt.mask}</code> <span className="muted small">{fmtPct(pt.share, 0)}</span></li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {rowCount != null && <p className="muted small">Measured over {fmtNumber(rowCount)} rows.</p>}
    </div>
  );
}

/** When and over what the table was profiled; stale or truncated profiles say so. */
export function profileMetaText(meta: ProfileMeta | null | undefined, fmtDate: (d: string | null | undefined) => string): string | null {
  if (!meta?.profiled_at) return null;
  const over = meta.rows_profiled != null ? ` over ${fmtNumber(meta.rows_profiled)} rows` : "";
  const where = meta.source === "snapshot" ? " of the staged copy" : "";
  const cut = meta.truncated ? " (the staged copy is truncated, so counts are partial)" : meta.sampled ? " (sampled)" : "";
  return `Profiled ${fmtDate(meta.profiled_at)}${over}${where}${cut}.`;
}
