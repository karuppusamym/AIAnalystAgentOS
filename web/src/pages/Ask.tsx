import { useState, type FormEvent, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api, errorMessage, type AskResponse, type QueryResult, type SqlExplanation } from "../api";
import { ChartView } from "../components/Chart";
import { Card, CodeBlock, DataTable, ErrorBox, Field, KeyValue, Notice, PageHeader } from "../components/ui";
import { guessChart } from "../lib/charts";
import { fmtMs } from "../lib/format";

/** Reorder a result so the hinted x / y columns come first (chart builders read columns 0 and 1). */
export function projectForChart(res: QueryResult, x?: string | null, y?: string | null) {
  const xi = x ? res.columns.indexOf(x) : -1;
  const yi = y ? res.columns.indexOf(y) : -1;
  if (xi < 0 || yi < 0) return { columns: res.columns, rows: res.rows };
  return { columns: [res.columns[xi], res.columns[yi]], rows: res.rows.map((r) => [r[xi], r[yi]]) };
}

function ResultView({ res, hint }: { res: QueryResult; hint?: AskResponse["chart"] }) {
  const projected = projectForChart(res, hint?.x, hint?.y);
  const type = guessChart(projected.columns, projected.rows, hint?.type);
  return (
    <>
      <p className="muted small">{res.row_count} rows{res.truncated ? " (truncated by policy max_rows)" : ""} · query <code>{res.query_id}</code>
        {res.duration_ms !== undefined ? ` · ${fmtMs(res.duration_ms)}` : ""}{res.cache_hit ? " · cache hit" : ""}</p>
      {type && type !== "table" && <ChartView type={type} preview={projected} height={300} showTable={false} />}
      <DataTable columns={res.columns} rows={res.rows} maxRows={200} caption="Query result" />
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

/** The deterministic explanation of a statement and the gateway's verdict — nothing is executed. */
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
      <p className="muted small">Explained deterministically from the parsed SQL — no model call and nothing executed.</p>
    </section>
  );
}

export function AskPage() {
  const { wsId = "" } = useParams();
  const [question, setQuestion] = useState("");
  const [ask, setAsk] = useState<AskResponse | null>(null);
  const [askErr, setAskErr] = useState<unknown>(null);
  const [asking, setAsking] = useState(false);
  const [sql, setSql] = useState("");
  const [maxRows, setMaxRows] = useState(500);
  const [qres, setQres] = useState<QueryResult | null>(null);
  const [qErr, setQErr] = useState<unknown>(null);
  const [running, setRunning] = useState(false);
  const [explain, setExplain] = useState<SqlExplanation | null>(null);
  const [explaining, setExplaining] = useState(false);

  const submitAsk = async (e: FormEvent) => {
    e.preventDefault();
    setAsking(true);
    setAskErr(null);
    setAsk(null);
    try {
      setAsk(await api.ask(wsId, question.trim()));
    } catch (err) {
      setAskErr(err);
    } finally {
      setAsking(false);
    }
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
      <PageHeader title="Ask" subtitle="Natural-language questions answered by the SQL agent — through the same governed gateway as every run." />
      <Card title="Ask a question">
        <form className="form" onSubmit={submitAsk}>
          <Field label="Question" htmlFor="ask-q">
            <textarea id="ask-q" rows={2} value={question} onChange={(e) => setQuestion(e.target.value)} required
              placeholder="How many P1 incidents were opened per month this year?" />
          </Field>
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={asking || !question.trim()}>{asking ? "Thinking…" : "Ask"}</button>
          </div>
        </form>
        <GatewayError error={askErr} />
        {ask && (
          <div className="stack">
            {ask.explanation && <p>{ask.explanation}</p>}
            <CodeBlock code={ask.sql} label={`SQL${ask.model ? ` · ${ask.model}` : ""}`} />
            {ask.attempts.length > 0 && (
              <Notice tone="warning">
                <strong>{ask.attempts.length} repair attempt{ask.attempts.length > 1 ? "s" : ""}</strong> — the gateway rejected earlier SQL and the agent repaired it:
                <ol className="small">
                  {ask.attempts.map((a, i) => <li key={i}><code className="clamp-2">{a.sql}</code><div className="warn-text">{a.error}</div></li>)}
                </ol>
              </Notice>
            )}
            <ResultView res={ask.result} hint={ask.chart} />
          </div>
        )}
      </Card>

      <Card title="SQL console">
        <form className="form" onSubmit={submitSql}>
          <Field label="SQL (read-only)" htmlFor="sql" hint="Same gateway, scope and audit as the agents: no bypass.">
            <textarea id="sql" className="mono" rows={6} value={sql} onChange={(e) => setSql(e.target.value)} spellCheck={false} required
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
  );
}
