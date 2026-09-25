import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ApiError, api, errorMessage, streamAskTurn, type AskInspector, type AskPromotion, type AskStage, type AskThreadDetail, type AskTurn,
  type ChartHint, type Dict, type QueryResult, type SqlExplanation,
} from "../api";
import { ChartView } from "../components/Chart";
import {
  Card, CodeBlock, DataTable, EmptyState, ErrorBox, Field, KeyValue, Loading, Notice, PageHeader, StateView, Tabs, TechnicalDetails,
} from "../components/ui";
import {
  PROMOTE_LABELS, canPromote, decisionLine, groupThreads, promotionText, provenancePills, refusalView, stalenessPill, topProbabilities,
  type Pill,
} from "../lib/ask";
import { guessChart } from "../lib/charts";
import { fmtDate, fmtMs, fmtNumber, fmtUsd } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";

/** Reorder a result so the hinted x / y columns come first (chart builders read columns 0 and 1). */
export function projectForChart(res: QueryResult, x?: string | null, y?: string | null) {
  const xi = x ? res.columns.indexOf(x) : -1;
  const yi = y ? res.columns.indexOf(y) : -1;
  if (xi < 0 || yi < 0) return { columns: res.columns, rows: res.rows };
  return { columns: [res.columns[xi], res.columns[yi]], rows: res.rows.map((r) => [r[xi], r[yi]]) };
}

/** Answer card body: an ECharts visual when the shape allows one, and always the table. */
function ResultView({ res, hint, caption = "Query result" }: { res: QueryResult; hint?: ChartHint; caption?: string }) {
  const projected = projectForChart(res, hint?.x, hint?.y);
  const type = guessChart(projected.columns, projected.rows, hint?.type);
  return (
    <>
      <p className="muted small">{res.row_count} rows{res.truncated ? " (truncated by policy max_rows)" : ""} · query <code>{res.query_id}</code>
        {res.duration_ms !== undefined && res.duration_ms !== null ? ` · ${fmtMs(res.duration_ms)}` : ""}{res.cache_hit ? " · cache hit" : ""}</p>
      {type && type !== "table" && <ChartView type={type} preview={projected} height={280} showTable={false} />}
      <DataTable columns={res.columns} rows={res.rows} maxRows={200} caption={caption} />
    </>
  );
}

function GatewayError({ error }: { error: unknown }) {
  if (!error) return null;
  if (error instanceof ApiError && error.code === "sql_rejected") {
    return (
      <div className="alert alert-danger" role="alert">
        <strong>Rejected by the query gateway.</strong> {error.message}
        <p className="small muted">Only read-only SELECTs over the workspace's selected assets are allowed; restricted and PII columns are withheld per policy.</p>
      </div>
    );
  }
  return <ErrorBox error={errorMessage(error)} />;
}

/** The deterministic explanation of a statement, the gateway's verdict and its plan — nothing is executed. */
export function ExplainView({ ex }: { ex: SqlExplanation }) {
  const g = ex.gateway;
  const list = (xs: string[] | undefined) => (xs?.length ? xs.join(", ") : null);
  const items: [string, ReactNode][] = [];
  if (ex.tables?.length) items.push(["Tables", <span key="t">{ex.tables.map((t) => <code key={t} className="tag">{t}</code>)}</span>]);
  if (ex.joins?.length) {
    items.push(["Joins", <ul key="j" className="list compact">{ex.joins.map((j, i) => (
      <li key={i} className="small"><code>{j.table}</code> ({j.kind}){j.on ? <> on <code>{j.on}</code></> : null}</li>
    ))}</ul>]);
  }
  if (ex.filter) items.push(["Filter", <code key="f">{ex.filter}</code>]);
  if (ex.group_by?.length) items.push(["Group by", list(ex.group_by)]);
  if (ex.aggregations?.length) items.push(["Aggregations", list(ex.aggregations)]);
  if (ex.having) items.push(["Having", <code key="h">{ex.having}</code>]);
  if (ex.order_by?.length) items.push(["Order by", list(ex.order_by)]);
  if (ex.limit) items.push(["Limit", ex.limit]);
  if (ex.ctes?.length) items.push(["Named subqueries", list(ex.ctes)]);
  const plan = ex.plan;
  return (
    <section className="stack explain" aria-label="Query explanation">
      {g.accepted ? (
        <Notice tone="success"><strong>The gateway would accept this query.</strong> It is read-only and stays within your scope.</Notice>
      ) : (
        <div className="alert alert-danger" role="alert">
          <strong>The gateway would reject this query{g.code ? ` (${g.code})` : ""}.</strong> {g.reason}
        </div>
      )}
      <p>{ex.summary}</p>
      {items.length > 0 && <KeyValue items={items} />}
      {plan && (plan.available ? (
        <div aria-label="Source plan">
          <p className="small"><strong>Source plan</strong> (EXPLAIN through the gateway, not run): {plan.node} ·
            about {fmtNumber(plan.estimated_rows, 0)} rows · cost {fmtNumber(plan.total_cost, 1)}</p>
          {plan.nodes?.length ? <p className="small muted">Steps: {plan.nodes.join(" → ")}</p> : null}
        </div>
      ) : <p className="small muted">No source plan: {plan.reason}</p>)}
      <p className="muted small">Explained deterministically from the parsed SQL — no model call, and no statement executed.</p>
    </section>
  );
}

// ------------------------------------------------------------------------------------ pills, stages, refusals
function Pills({ pills, label }: { pills: Pill[]; label: string }) {
  return (
    <ul className="pill-row" aria-label={label}>
      {pills.map((p, i) => (
        <li key={i} className={`badge badge-${p.tone}`} title={p.title}><span className="badge-dot" aria-hidden="true" />{p.label}</li>
      ))}
    </ul>
  );
}

function Stages({ stages, live }: { stages: AskStage[]; live: boolean }) {
  if (!stages.length) return null;
  return (
    <ol className="ask-stages small" aria-label="Progress" aria-live={live ? "polite" : undefined}>
      {stages.filter((s) => s.key !== "done" || !live).map((s, i, all) => (
        <li key={i} className={live && i === all.length - 1 ? "ask-stage-current" : "muted"}>{s.text}</li>
      ))}
    </ol>
  );
}

function ParameterForm({ turn, onSubmit, busy }: { turn: AskTurn; onSubmit: (p: Dict) => void; busy: boolean }) {
  const missing = turn.refusal?.details?.missing ?? [];
  const [values, setValues] = useState<Record<string, string>>({});
  const id = useId();
  return (
    <form className="form-row" onSubmit={(e) => { e.preventDefault(); onSubmit({ ...(turn.refusal?.details?.parameters ?? {}), ...values }); }}>
      {missing.map((m) => (
        <Field key={m.name} label={m.name.replace(/_/g, " ")} htmlFor={`${id}-${m.name}`}>
          {m.values?.length ? (
            <select id={`${id}-${m.name}`} required value={values[m.name] ?? ""} onChange={(e) => setValues({ ...values, [m.name]: e.target.value })}>
              <option value="" disabled>Choose…</option>
              {m.values.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          ) : (
            <input id={`${id}-${m.name}`} required value={values[m.name] ?? ""} onChange={(e) => setValues({ ...values, [m.name]: e.target.value })} />
          )}
        </Field>
      ))}
      <div className="form-actions"><button type="submit" className="btn btn-primary" disabled={busy}>Answer with these values</button></div>
    </form>
  );
}

function RefusalState({ turn, ws, busy, onParameters, onRephrase, onExplain, onRetry }: {
  turn: AskTurn; ws: string; busy: boolean; onParameters: (p: Dict) => void; onRephrase: () => void; onExplain: (sql: string) => void; onRetry: () => void;
}) {
  const r = turn.refusal;
  const view = refusalView(r);
  const lastSql = turn.attempts.length ? turn.attempts[turn.attempts.length - 1].sql : turn.sql;
  let action: ReactNode = null;
  if (view.action === "rephrase") action = <button type="button" className="btn btn-sm" onClick={onRephrase}>{view.actionLabel}</button>;
  else if (view.action === "explain") action = <button type="button" className="btn btn-sm" onClick={() => onExplain(lastSql ?? "")}>{view.actionLabel}</button>;
  else if (view.action === "sources") action = <Link className="btn btn-sm" to={to.sources(ws)}>{view.actionLabel}</Link>;
  else if (view.action === "access") action = <Link className="btn btn-sm" to={to.governance(ws)}>{view.actionLabel}</Link>;
  else if (view.action === "retry") action = <button type="button" className="btn btn-sm" onClick={onRetry} disabled={busy}>{view.actionLabel}</button>;
  return (
    <div data-refusal={r?.kind ?? "failed"}>
      <StateView kind={view.state} title={r?.title ?? "Not answered"} action={action}>
        {r?.message && <p>{r.message}</p>}
        <p><strong>What to do:</strong> {r?.remedy ?? "Rephrase the question."}</p>
        {view.action === "parameters" && <ParameterForm turn={turn} onSubmit={onParameters} busy={busy} />}
      </StateView>
    </div>
  );
}

// ------------------------------------------------------------------------------------ promote
type MonitorKind = "metric_drift" | "metric_threshold";

function PromoteBar({ turn, onRecorded }: { turn: AskTurn; onRecorded: (p: AskPromotion) => void }) {
  const { wsId = "" } = useParams();
  const navigate = useNavigate();
  const act = useAction();
  const [monitorOpen, setMonitorOpen] = useState(false);
  const [kind, setKind] = useState<MonitorKind>("metric_drift");
  const [op, setOp] = useState(">");
  const [value, setValue] = useState("");
  const [grain, setGrain] = useState<"day" | "week" | "month">("week");
  const id = useId();

  const promote = async (body: Parameters<typeof api.promoteTurn>[1]) => {
    const p = await act.run(() => api.promoteTurn(turn.id, body));
    if (!p) return;
    onRecorded(p);
    if (p.target === "investigate") navigate(to.run(wsId, p.id));
  };
  const submitMonitor = async (e: FormEvent) => {
    e.preventDefault();
    await promote(kind === "metric_threshold"
      ? { target: "monitor", kind, op, value: Number(value), grain }
      : { target: "monitor", kind, grain });
    setMonitorOpen(false);
  };
  return (
    <section className="stack" aria-label="Promote this answer">
      <div className="btn-row">
        <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void promote({ target: "verified_query" })}>{PROMOTE_LABELS.verified_query}</button>
        <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void promote({ target: "metric" })}>{PROMOTE_LABELS.metric}</button>
        <button type="button" className="btn btn-sm" disabled={act.busy} aria-expanded={monitorOpen} onClick={() => setMonitorOpen((o) => !o)}>{PROMOTE_LABELS.monitor}</button>
        <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void promote({ target: "dashboard" })}>{PROMOTE_LABELS.dashboard}</button>
        <button type="button" className="btn btn-sm btn-primary" disabled={act.busy} onClick={() => void promote({ target: "investigate" })}>{PROMOTE_LABELS.investigate}</button>
      </div>
      {monitorOpen && (
        <form className="form-row" aria-label="New monitor" onSubmit={submitMonitor}>
          <Field label="Watch for" htmlFor={`${id}-kind`}>
            <select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value as MonitorKind)}>
              <option value="metric_drift">Unusual change (drift)</option>
              <option value="metric_threshold">A threshold</option>
            </select>
          </Field>
          {kind === "metric_threshold" && (
            <>
              <Field label="Operator" htmlFor={`${id}-op`}>
                <select id={`${id}-op`} value={op} onChange={(e) => setOp(e.target.value)}>
                  {[">", ">=", "<", "<="].map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </Field>
              <Field label="Threshold" htmlFor={`${id}-value`}>
                <input id={`${id}-value`} type="number" required value={value} onChange={(e) => setValue(e.target.value)} />
              </Field>
            </>
          )}
          <Field label="Every" htmlFor={`${id}-grain`}>
            <select id={`${id}-grain`} value={grain} onChange={(e) => setGrain(e.target.value as "day" | "week" | "month")}>
              <option value="day">day</option><option value="week">week</option><option value="month">month</option>
            </select>
          </Field>
          <div className="form-actions"><button type="submit" className="btn btn-primary btn-sm" disabled={act.busy}>Create monitor</button></div>
        </form>
      )}
      <ErrorBox error={act.error} />
      {turn.promotions.length > 0 && (
        <ul className="list compact" aria-label="Promotions">
          {turn.promotions.map((p, i) => (
            <li key={i} className="list-item small" role="status">
              {promotionText(p)}
              {p.target === "monitor" && <Link to={to.monitoring(wsId)}>Open monitors</Link>}
              {p.status === "approval_required" && <Link to={to.approvals(wsId)}>Open approvals</Link>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ------------------------------------------------------------------------------------ turn
function TurnView({ turn, selected, onSelect, busy, onParameters, onRephrase, onExplain, onRetry, onRecorded }: {
  turn: AskTurn; selected: boolean; onSelect: () => void; busy: boolean; onParameters: (p: Dict) => void; onRephrase: () => void;
  onExplain: (sql: string) => void; onRetry: () => void; onRecorded: (p: AskPromotion) => void;
}) {
  const { wsId = "" } = useParams();
  return (
    <article className={`ask-turn ${selected ? "ask-turn-selected" : ""}`} aria-label={`Question ${turn.seq}`}>
      <header className="ask-question">
        <p><strong>{turn.question}</strong></p>
        <button type="button" className="btn btn-xs btn-ghost" aria-pressed={selected} onClick={onSelect}>Inspect</button>
      </header>
      {turn.status === "answered" && turn.result ? (
        <div className="stack">
          <Pills label="Provenance" pills={[...provenancePills(turn), stalenessPill(turn.staleness)]} />
          {turn.explanation && <p>{turn.explanation}</p>}
          <ResultView res={turn.result} hint={turn.chart} caption={turn.question} />
          {turn.sql && <CodeBlock code={turn.sql} label={`SQL${turn.model ? ` · ${turn.model}` : turn.answered_by === "registry" ? " · verified query" : ""}`} />}
          {canPromote(turn) && <PromoteBar turn={turn} onRecorded={onRecorded} />}
        </div>
      ) : (
        <RefusalState turn={turn} ws={wsId} busy={busy} onParameters={onParameters} onRephrase={onRephrase} onExplain={onExplain} onRetry={onRetry} />
      )}
    </article>
  );
}

// ------------------------------------------------------------------------------------ inspector
type InspectorTab = "result" | "sql" | "evidence" | "decision";

function Inspector({ turn }: { turn: AskTurn }) {
  const [tab, setTab] = useState<InspectorTab>("result");
  const data = useAsync<AskInspector>(() => api.askInspector(turn.id), [turn.id, turn.promotions.length]);
  const d = data.data;
  const p = turn.provenance ?? {};
  return (
    <aside className="card ask-inspector" aria-label="Answer inspector">
      <div className="card-body stack">
        <Tabs<InspectorTab> value={tab} onChange={setTab}
          tabs={[{ id: "result", label: "Result" }, { id: "sql", label: "SQL" }, { id: "evidence", label: "Evidence" }, { id: "decision", label: "Decision" }]} />
        {data.loading && !d && <Loading label="Loading the answer's record…" />}
        <ErrorBox error={data.error} onRetry={data.reload} />
        {tab === "result" && (turn.result
          ? <KeyValue items={[
            ["Rows", `${turn.result.row_count}${turn.result.truncated ? " (truncated)" : ""}`],
            ["Columns", turn.result.columns.join(", ")],
            ["Query", <code key="q">{turn.result.query_id}</code>],
            ["Answered in", fmtMs(turn.latency_ms)],
            ["Answered by", turn.answered_by === "registry" ? "verified query (no model)" : turn.answered_by === "model" ? "generated SQL" : "—"],
          ]} />
          : <EmptyState title="No result">This question was not answered; the refusal says why.</EmptyState>)}
        {tab === "sql" && (
          <div className="stack">
            {turn.sql ? <CodeBlock code={turn.sql} label="SQL as written" /> : <EmptyState title="No SQL ran" />}
            {d?.query?.executed_sql && d.query.executed_sql !== turn.sql && <CodeBlock code={d.query.executed_sql} label="SQL as the gateway ran it (row limit applied)" />}
            {d?.query && <p className="small muted">Gateway audit: {d.query.status}, {d.query.row_count} rows, {fmtMs(d.query.duration_ms)}, fingerprint <code>{String(d.query.fingerprint ?? "").slice(0, 12)}</code></p>}
            {turn.attempts.length > 0 && (
              <Notice tone="warning">
                <strong>{turn.attempts.length} repair attempt{turn.attempts.length > 1 ? "s" : ""}</strong> — the gateway rejected earlier SQL:
                <ol className="small">{turn.attempts.map((a, i) => <li key={i}><code className="clamp-2">{a.sql}</code><div className="warn-text">{a.error}</div></li>)}</ol>
              </Notice>
            )}
          </div>
        )}
        {tab === "evidence" && (
          <div className="stack">
            {(p.assets ?? []).length > 0 ? (
              <DataTable caption="Tables the answer read" columns={["Table", "Source", "Mode", "Data as of", "Rows"]}
                rows={(p.assets ?? []).map((a) => [a.asset, a.source_name ?? "unknown", a.execution_mode ?? "unknown",
                  a.freshness_at ? fmtDate(a.freshness_at) : "unknown", a.row_count ?? "unknown"])} />
            ) : <EmptyState title="No tables recorded" />}
            <KeyValue items={[
              ["Freshness", stalenessPill(turn.staleness).label],
              ["Verified query", p.verified_query ? `${p.verified_query.name}${p.verified_query.score ? ` (match ${p.verified_query.score})` : ""}` : "—"],
              ["Result hash", p.result_hash ? <code key="h">{String(p.result_hash).slice(0, 16)}</code> : "—"],
            ]} />
            <div>
              <h3 className="h-sm">Knowledge used</h3>
              {d?.receipts?.length ? (
                <ul className="list compact">{d.receipts.map((r, i) => (
                  <li key={i} className="list-item small">{String(r.title ?? r.name ?? r.id ?? r.kind ?? "context item")}
                    {r.kind ? <span className="tag">{String(r.kind)}</span> : null}{r.version ? <span className="tag">v{String(r.version)}</span> : null}</li>
                ))}</ul>
              ) : <p className="small muted">No context receipts: no model prompt was compiled for this answer.</p>}
            </div>
          </div>
        )}
        {tab === "decision" && (
          <div className="stack">
            <KeyValue items={[["Ladder rung", turn.answered_by ?? "—"], ["Route", turn.route ?? "—"]]} />
            {d?.decisions?.length ? (
              <ul className="list compact" aria-label="Decisions">{d.decisions.map((row) => (
                <li key={row.id} className="list-item small">
                  <span>{decisionLine(row)}{topProbabilities(row).length > 0 && (
                    <span className="muted"> · {topProbabilities(row).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(", ")}</span>)}</span>
                  <span className="tag">{row.authority}</span>
                </li>
              ))}</ul>
            ) : <p className="small muted">{turn.decisions.length ? turn.decisions.map((x) => `${x.purpose}: ${String(x.value)} (${x.backend})`).join("; ") : "No decisions recorded."}</p>}
            {d?.model_calls?.length ? (
              <DataTable caption="Model calls" columns={["Purpose", "Status", "Rung", "Model", "Tokens", "Cost"]}
                rows={d.model_calls.map((c) => [c.purpose, c.status, c.answered_by ?? "—", c.model, c.input_tokens + c.output_tokens, fmtUsd(c.cost_usd)])} />
            ) : <p className="small muted">No model calls.</p>}
            {d && <TechnicalDetails value={{ decisions: d.decisions, model_calls: d.model_calls }} />}
          </div>
        )}
      </div>
    </aside>
  );
}

// ------------------------------------------------------------------------------------ page
export function AskPage() {
  const { wsId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const threadId = params.get("thread");
  const [search, setSearch] = useState("");
  const threads = useAsync(() => api.askThreads(wsId, search.trim() || undefined), [wsId, search]);
  const [thread, setThread] = useState<AskThreadDetail | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [live, setLive] = useState<AskStage[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  const [askErr, setAskErr] = useState<unknown>(null);
  const [asking, setAsking] = useState(false);
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const consoleRef = useRef<HTMLTextAreaElement>(null);

  const [sql, setSql] = useState("");
  const [maxRows, setMaxRows] = useState(500);
  const [qres, setQres] = useState<QueryResult | null>(null);
  const [qErr, setQErr] = useState<unknown>(null);
  const [running, setRunning] = useState(false);
  const [explain, setExplain] = useState<SqlExplanation | null>(null);
  const [explaining, setExplaining] = useState(false);

  useEffect(() => {
    let stale = false;
    if (!threadId) {
      setThread(null);
      return;
    }
    if (thread?.id === threadId) return;
    api.askThread(threadId).then((t) => {
      if (stale) return;
      setThread(t);
      setSelected(t.turns.length ? t.turns[t.turns.length - 1].id : null);
    }).catch((err) => { if (!stale) setAskErr(err); });
    return () => { stale = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  const selectedTurn = useMemo(() => thread?.turns.find((t) => t.id === selected) ?? null, [thread, selected]);
  const groups = useMemo(() => groupThreads(threads.data ?? []), [threads.data]);

  const ask = async (text: string, parameters?: Dict) => {
    const q = text.trim();
    if (!q) return;
    setAsking(true);
    setAskErr(null);
    setLive([]);
    setPending(q);
    try {
      let current = thread;
      if (!current) {
        current = await api.createAskThread(wsId);
        setThread(current);
        setParams((prev) => { const n = new URLSearchParams(prev); n.set("thread", current!.id); return n; }, { replace: true });
      }
      const turn = await streamAskTurn(current.id, q, parameters, { onStage: (s) => setLive((xs) => [...xs, s]) });
      setThread((t) => (t ? { ...t, title: t.turns.length ? t.title : q, turns: [...t.turns, turn] } : t));
      setSelected(turn.id);
      setQuestion("");
      void threads.reload();
    } catch (err) {
      setAskErr(err);
    } finally {
      setAsking(false);
      setPending(null);
    }
  };

  const recordPromotion = (turnId: string, p: AskPromotion) =>
    setThread((t) => (t ? { ...t, turns: t.turns.map((x) => (x.id === turnId ? { ...x, promotions: [...x.promotions, p] } : x)) } : t));

  const openThread = (id: string | null) => {
    setParams((prev) => { const n = new URLSearchParams(prev); if (id) n.set("thread", id); else n.delete("thread"); return n; });
    if (!id) { setThread(null); setSelected(null); }
  };

  const explainSql = (text: string) => {
    setSql(text);
    consoleRef.current?.scrollIntoView?.({ block: "center" });
    consoleRef.current?.focus();
  };

  const runExplain = async () => {
    setExplaining(true);
    setQErr(null);
    setExplain(null);
    try {
      setExplain(await api.explainQuery(wsId, sql, maxRows));
    } catch (err) {
      setQErr(err);
    } finally {
      setExplaining(false);
    }
  };

  const submitSql = async (e: FormEvent) => {
    e.preventDefault();
    setExplain(null);
    setRunning(true);
    setQErr(null);
    setQres(null);
    try {
      setQres(await api.query(wsId, sql, maxRows));
    } catch (err) {
      setQErr(err);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page">
      <PageHeader title="Ask" subtitle="Questions answered from governed data: verified answers first, generated SQL only through the same gateway as every run." />
      <div className="ask-layout">
        <nav className="card ask-threads" aria-label="Threads">
          <div className="card-body stack">
            <button type="button" className="btn btn-sm" onClick={() => openThread(null)}>New thread</button>
            <Field label="Search threads" htmlFor="ask-search">
              <input id="ask-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Words from a question" />
            </Field>
            <ErrorBox error={threads.error} onRetry={threads.reload} />
            {groups.length === 0 && !threads.loading && <p className="small muted">{search ? "No thread matches." : "No threads yet."}</p>}
            {groups.map((g) => (
              <div key={g.label}>
                <h2 className="h-sm muted">{g.label}</h2>
                <ul className="list selectable">
                  {g.items.map((t) => (
                    <li key={t.id}>
                      <button type="button" className={`list-button ${t.id === threadId ? "active" : ""}`} aria-current={t.id === threadId ? "true" : undefined}
                        onClick={() => openThread(t.id)}>
                        <span className="clamp-2">{t.title}</span>
                        <span className="small muted">{t.turn_count ?? 0} question{t.turn_count === 1 ? "" : "s"}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </nav>

        <div className="stack ask-main">
          <Card title={thread && thread.turns.length ? thread.title : "Ask a question"}>
            <div className="stack">
              {thread?.turns.map((t) => (
                <TurnView key={t.id} turn={t} selected={t.id === selected} onSelect={() => setSelected(t.id)} busy={asking}
                  onParameters={(p) => void ask(t.question, p)} onRephrase={() => { setQuestion(t.question); questionRef.current?.focus(); }}
                  onExplain={explainSql} onRetry={() => void ask(t.question, t.parameters)} onRecorded={(p) => recordPromotion(t.id, p)} />
              ))}
              {pending && (
                <article className="ask-turn" aria-label="Question in progress" aria-busy="true">
                  <p><strong>{pending}</strong></p>
                  {live.length ? <Stages stages={live} live /> : <Loading label="Starting…" />}
                </article>
              )}
              <form className="form" onSubmit={(e) => { e.preventDefault(); void ask(question); }}>
                <Field label="Question" htmlFor="ask-q">
                  <textarea id="ask-q" ref={questionRef} rows={2} value={question} onChange={(e) => setQuestion(e.target.value)} required
                    placeholder="How many P1 incidents were opened per month this year?"
                    onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) (e.currentTarget.form as HTMLFormElement | null)?.requestSubmit(); }} />
                </Field>
                <div className="form-actions">
                  <button type="submit" className="btn btn-primary" disabled={asking || !question.trim()}>{asking ? "Thinking…" : "Ask"}</button>
                </div>
              </form>
              <GatewayError error={askErr} />
            </div>
          </Card>

          <Card title="SQL console">
            <form className="form" onSubmit={submitSql}>
              <Field label="SQL (read-only)" htmlFor="sql" hint="Paste SQL and Explain it first: the validator and the source's plan, nothing executed. Run uses the same gateway, scope and audit as the agents.">
                <textarea id="sql" ref={consoleRef} className="mono" rows={6} value={sql} onChange={(e) => setSql(e.target.value)} spellCheck={false} required
                  placeholder="SELECT priority, COUNT(*) FROM src_xxx.incident GROUP BY 1 ORDER BY 2 DESC"
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) (e.currentTarget.form as HTMLFormElement | null)?.requestSubmit(); }} />
              </Field>
              <div className="form-actions">
                <label className="inline-field small">Max rows <input type="number" min={1} max={50000} value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value))} /></label>
                <button type="button" className="btn" onClick={() => void runExplain()} disabled={explaining || running || !sql.trim()}
                  title="Explain the query and check it against the gateway without running it">{explaining ? "Explaining…" : "Explain"}</button>
                <button type="submit" className="btn btn-primary" disabled={running || !sql.trim()}>{running ? "Running…" : "Run (Ctrl+Enter)"}</button>
              </div>
            </form>
            <GatewayError error={qErr} />
            {explain && <ExplainView ex={explain} />}
            {qres && <ResultView res={qres} />}
          </Card>
        </div>

        {selectedTurn ? <Inspector turn={selectedTurn} /> : (
          <aside className="card ask-inspector" aria-label="Answer inspector">
            <div className="card-body"><EmptyState title="No answer selected">Ask a question, or pick one, to see its result, SQL, evidence and the decisions behind it.</EmptyState></div>
          </aside>
        )}
      </div>
    </div>
  );
}
