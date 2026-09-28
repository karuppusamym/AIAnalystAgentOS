import { useState, type FormEvent } from "react";
import type { AnalystStep, AskTurn } from "../api";
import { guessChart } from "../lib/charts";
import { fmtNumber, fmtPct } from "../lib/format";
import { ChartView } from "./Chart";
import { CodeBlock, DataTable, Notice, StatusBadge, Tag } from "./ui";

/**
 * An Analyst-mode answer (Ask, mode "analyst"): the synthesis first, each evidence line citing the
 * step it came from, then the steps themselves — what each asked, the SQL that ran through the
 * gateway, the facts computed in code from its result, its checks, trend and drivers. A step can be
 * re-run with edited SQL; the synthesis then says it is stale until it is rewritten.
 */
export function AnalystAnswer({ turn, busy, onRerunStep, onResynthesize, onAsk }: {
  turn: AskTurn; busy: boolean; onRerunStep: (n: number, sql?: string) => void; onResynthesize: () => void; onAsk: (q: string) => void;
}) {
  const a = turn.analysis!;
  const syn = a.synthesis;
  const stepAnchor = (n: number) => `${turn.id}-step-${n}`;
  const cite = (n: number) => (
    <a key={n} href={`#${stepAnchor(n)}`} className="step-cite" onClick={(e) => {
      e.preventDefault();
      const el = document.getElementById(stepAnchor(n));
      if (el instanceof HTMLDetailsElement) el.open = true;
      el?.scrollIntoView({ block: "center", behavior: "smooth" });
    }}>step {n}</a>
  );
  return (
    <div className="analyst-answer stack">
      {syn.stale && (
        <Notice tone="warning">
          A step was re-run since this answer was written, so it may no longer match.{" "}
          <button type="button" className="btn btn-xs" disabled={busy} onClick={onResynthesize}>Rewrite the answer</button>
        </Notice>
      )}
      <section className="analyst-synthesis" aria-label="Answer">
        <p className="analyst-lead">{withCitations(syn.answer ?? syn.text.split("\n")[0], cite)}</p>
        {!!syn.evidence?.length && (
          <ul className="analyst-evidence">
            {syn.evidence.map((e, i) => <li key={i}>{withCitations(e.text, cite)}</li>)}
          </ul>
        )}
        {!!syn.caveats?.length && (
          <div className="analyst-caveats">
            <strong className="small">Keep in mind</strong>
            <ul>{syn.caveats.map((c, i) => <li key={i} className="small">{withCitations(c, cite)}</li>)}</ul>
          </div>
        )}
        <p className="muted small">
          {syn.origin === "model" ? "Written by a model and checked: every number matches a computed fact." : "Written from the facts computed in code; no model wrote it."}
          {syn.rejected ? ` A model draft was not used: ${syn.rejected}` : ""}
        </p>
      </section>

      <details className="analyst-plan">
        <summary>How it was worked out: {a.steps.length} step{a.steps.length === 1 ? "" : "s"} ({a.plan.origin === "model" ? "planned by a model, validated" : "planned by rules"})</summary>
        <p className="small">{a.plan.approach}</p>
        {!!a.plan.assumptions?.length && <p className="small">Assumed: {a.plan.assumptions.join("; ")}</p>}
      </details>

      <ol className="analyst-steps">
        {a.steps.map((s) => (
          <StepCard key={s.n} step={s} anchor={stepAnchor(s.n)} headline={s.n === a.headline_step} busy={busy}
            stale={!!syn.stale_steps?.includes(s.n)} onRerun={(sql) => onRerunStep(s.n, sql)} />
        ))}
      </ol>

      {!!a.follow_ups.length && (
        <div className="chip-row" aria-label="Follow-up questions">
          {a.follow_ups.map((q) => <button key={q} type="button" className="btn btn-sm" disabled={busy} onClick={() => onAsk(q)}>{q}</button>)}
        </div>
      )}
    </div>
  );
}

/** "(step 2)" in the synthesis text becomes a link to that step. */
function withCitations(text: string, cite: (n: number) => JSX.Element) {
  const parts = text.split(/\(steps? (\d+(?:\s*(?:,|and)\s*\d+)*)\)/g);
  return parts.map((p, i) => {
    if (i % 2 === 0) return <span key={i}>{p}</span>;
    const ns = p.split(/\s*(?:,|and)\s*/).map(Number).filter(Number.isFinite);
    return <span key={i} className="step-cites">({ns.map((n, k) => <span key={n}>{k > 0 && ", "}{cite(n)}</span>)})</span>;
  });
}

function StepCard({ step: s, anchor, headline, busy, stale, onRerun }: {
  step: AnalystStep; anchor: string; headline: boolean; busy: boolean; stale: boolean; onRerun: (sql?: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [sql, setSql] = useState(s.sql ?? "");
  const suspect = (s.checks ?? []).filter((c) => c.status !== "pass");
  const res = s.result;
  const chartType = res ? guessChart(res.columns, res.rows, s.chart?.type) : null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    onRerun(sql.trim() === (s.sql ?? "").trim() ? undefined : sql);
    setEditing(false);
  };
  return (
    <li>
      <details id={anchor} className="analyst-step" open={headline || s.status !== "answered"}>
        <summary>
          <span className="analyst-step-n">{s.n}</span>
          <span className="analyst-step-goal">{s.goal}</span>
          <StatusBadge status={s.status} />
          {suspect.length > 0 && <Tag tone="warning">{suspect.length} to check</Tag>}
          {s.governance === "governed" && <Tag tone="success">governed metric</Tag>}
          {stale && <Tag tone="warning">re-run</Tag>}
        </summary>
        <div className="stack analyst-step-body">
          <p className="small muted">Asked as: “{s.question}”{s.answered_by ? ` · answered by ${s.answered_by === "rules" ? "rules (no model)" : s.answered_by}` : ""}</p>
          {s.refusal && <Notice tone="warning">{s.refusal.message}</Notice>}
          {s.note && <p className="small muted">{s.note}</p>}
          {!!s.facts?.statements?.length && (
            <ul className="analyst-facts">{s.facts.statements.map((t, i) => <li key={i}>{t}</li>)}</ul>
          )}
          {s.series && (
            <p className="small">
              Trend over {s.series.points} periods ({s.series.first_period} to {s.series.last_period}): <strong>{s.series.direction ?? "—"}</strong>
              {s.series.slope_share_of_mean != null && <> ({fmtPct(s.series.slope_share_of_mean, 1)} of the average per period)</>}
              {!!s.series.anomalies?.length && <> · unusual: {s.series.anomalies.map((x) => `${x.period} (${x.direction})`).join(", ")}</>}
              {!!s.series.missing_periods?.length && <> · missing periods: {s.series.missing_periods.join(", ")}</>}
            </p>
          )}
          {s.comparison && s.comparison.change != null && (
            <p className="small">
              {s.comparison.previous_period ?? "Before"}: {fmtNumber(s.comparison.previous)} → {s.comparison.current_period ?? "after"}: {fmtNumber(s.comparison.current)}
              {" "}({s.comparison.change >= 0 ? "+" : ""}{fmtNumber(s.comparison.change)}{s.comparison.pct_change != null && `, ${fmtPct(s.comparison.pct_change, 1)}`})
            </p>
          )}
          {s.drivers && (s.drivers.drivers.length > 0 || s.drivers.offsets.length > 0) && <DriverTable drivers={s.drivers} />}
          {!!s.checks?.length && (
            <ul className="analyst-checks">
              {s.checks.map((c) => (
                <li key={c.code} className={c.status === "pass" ? "check-pass" : "check-suspect"}>
                  <span aria-hidden="true">{c.status === "pass" ? "✓" : "!"}</span> {c.note}
                </li>
              ))}
            </ul>
          )}
          {res && (
            <>
              {chartType && chartType !== "table" && <ChartView type={chartType} preview={{ columns: res.columns, rows: res.rows }} height={240} showTable={false} />}
              <DataTable columns={res.columns} rows={res.rows} maxRows={50} caption={s.goal} />
              {res.truncated && <p className="small muted">Showing the first rows of {fmtNumber(res.row_count)}.</p>}
            </>
          )}
          {s.sql && <CodeBlock code={s.sql} label="SQL (ran through the query gateway)" />}
          {s.status === "answered" && (
            <div className="btn-row">
              <button type="button" className="btn btn-sm" disabled={busy} onClick={() => onRerun()}>Re-run this step</button>
              <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setEditing((e) => !e)}>{editing ? "Cancel" : "Edit SQL and re-run"}</button>
            </div>
          )}
          {editing && (
            <form className="stack" onSubmit={submit} aria-label={`Edit the SQL of step ${s.n}`}>
              <label>SQL<textarea className="mono" rows={6} value={sql} onChange={(e) => setSql(e.target.value)} spellCheck={false} /></label>
              <p className="small muted">Runs read-only through the same gateway. The answer is marked out of date until you rewrite it.</p>
              <button type="submit" className="btn btn-primary btn-sm" disabled={busy || !sql.trim()}>Run</button>
            </form>
          )}
        </div>
      </details>
    </li>
  );
}

function DriverTable({ drivers }: { drivers: NonNullable<AnalystStep["drivers"]> }) {
  const rows = [...drivers.drivers.map((d) => ({ ...d, kind: "driver" })), ...drivers.offsets.map((d) => ({ ...d, kind: "offset" }))].slice(0, 8);
  return (
    <div className="table-wrap">
      <table className="table table-compact">
        <caption className="small">What moved the total (computed in code)</caption>
        <thead><tr><th>Member</th><th className="num">Before</th><th className="num">After</th><th className="num">Change</th><th className="num">Share of change</th><th /></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.member}>
              <td>{r.member}</td>
              <td className="num">{fmtNumber(r.previous)}</td>
              <td className="num">{fmtNumber(r.current)}</td>
              <td className="num">{r.change >= 0 ? "+" : ""}{fmtNumber(r.change)}</td>
              <td className="num">{r.share_of_change == null ? "—" : fmtPct(r.share_of_change, 0)}</td>
              <td className="small muted">{r.kind === "driver" ? "drove it" : "offset it"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(drivers.appeared?.length || drivers.disappeared?.length) ? (
        <p className="small muted">
          {drivers.appeared?.length ? `New: ${drivers.appeared.join(", ")}. ` : ""}{drivers.disappeared?.length ? `Gone: ${drivers.disappeared.join(", ")}.` : ""}
        </p>
      ) : null}
    </div>
  );
}
