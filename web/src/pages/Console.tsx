import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { to } from "../routes";
import { api, type ModelCall } from "../api";
import { Card, CodeBlock, EmptyState, ErrorBox, JsonView, Loading, PageHeader, Stat, StatusBadge, Tabs, Value } from "../components/ui";
import { fmtMs, fmtTime } from "../lib/format";
import { useAsync } from "../lib/hooks";

export const isJev = (c: Pick<ModelCall, "provider">) => c.provider === "typesafe";

type Tab = "messages" | "tools" | "models" | "queries";

export function ConsolePage() {
  const { wsId = "", runId = "" } = useParams();
  const data = useAsync(() => api.console(wsId, runId), [wsId, runId]);
  const [tab, setTab] = useState<Tab>("messages");
  const [agent, setAgent] = useState("");

  if (data.error && !data.data) return <div className="page"><ErrorBox error={data.error} onRetry={data.reload} /></div>;
  if (!data.data) return <div className="page"><Loading /></div>;
  const d = data.data;
  const agents = [...new Set([...d.messages.map((m) => m.agent_id), ...d.tool_calls.map((t) => t.agent_id ?? ""), ...d.model_calls.map((m) => m.agent_id ?? "")])]
    .filter(Boolean).sort();
  const f = <T extends { agent_id: string | null }>(xs: T[]) => (agent ? xs.filter((x) => x.agent_id === agent) : xs);

  return (
    <div className="page">
      <PageHeader title="Agent console" subtitle={<>Run <Link to={to.run(wsId, runId)}><code>{runId}</code></Link> — what each agent thought, called and queried.</>}
        actions={<button type="button" className="btn btn-sm" onClick={data.reload} disabled={data.loading}>{data.loading ? "Refreshing…" : "Refresh"}</button>} />
      <div className="stats-row card card-body">
        <Stat label="Cost" value={<Value value={d.cost?.usd} format="usd" />} />
        <Stat label="Tokens" value={<Value value={d.cost?.tokens} format="int" />} />
        <Stat label="Model calls" value={<Value value={d.cost?.model_calls} format="int" />} />
        <Stat label="JEV decisions" value={<Value value={d.cost?.jev_calls} format="int" />} hint="TypeSafe Jev" />
        <Stat label="Failed calls" value={<Value value={d.cost?.failed_calls} format="int" />} />
        <Stat label="Tool calls" value={d.tool_calls.length} />
        <Stat label="Queries" value={d.queries.length} />
      </div>
      <div className="toolbar">
        <Tabs value={tab} onChange={setTab} tabs={[
          { id: "messages", label: `Messages (${d.messages.length})` },
          { id: "tools", label: `Tool calls (${d.tool_calls.length})` },
          { id: "models", label: `Model calls (${d.model_calls.length})` },
          { id: "queries", label: `Queries (${d.queries.length})` },
        ]} />
        {tab !== "queries" && (
          <label className="inline-field small">Agent
            <select value={agent} onChange={(e) => setAgent(e.target.value)}>
              <option value="">all</option>
              {agents.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </label>
        )}
      </div>
      <Card>
        {tab === "messages" && (f(d.messages).length === 0 ? <EmptyState title="No messages" /> : (
          <ol className="timeline">
            {f(d.messages).map((m) => (
              <li key={m.id} className={`msg msg-${m.kind}`}>
                <div className="msg-head"><span className="muted small">{fmtTime(m.created_at)}</span> <strong>{m.agent_id}</strong> <span className="tag">{m.kind}</span></div>
                <div className="msg-body">{m.content}</div>
                {m.data && Object.keys(m.data).length > 0 && <JsonView value={m.data} collapsed label="data" />}
              </li>
            ))}
          </ol>
        ))}
        {tab === "tools" && (f(d.tool_calls).length === 0 ? <EmptyState title="No tool calls" /> : (
          <div className="table-wrap">
            <table className="table table-compact">
              <thead><tr><th>Time</th><th>Agent</th><th>Tool</th><th>Status</th><th>Decision</th><th className="num">Latency</th><th>Detail</th></tr></thead>
              <tbody>
                {f(d.tool_calls).map((t) => {
                  const dec = t.decision as { decision?: string; reasons?: string[]; risk_tier?: string };
                  return (
                    <tr key={t.id}>
                      <td className="small">{fmtTime(t.created_at)}</td>
                      <td>{t.agent_id}</td>
                      <td><code>{t.tool_id}</code></td>
                      <td><StatusBadge status={t.status} /></td>
                      <td>{dec?.decision ? <StatusBadge status={dec.decision} /> : "—"}{dec?.reasons?.length ? <div className="muted small">{dec.reasons.join(", ")}</div> : null}</td>
                      <td className="num">{fmtMs(t.latency_ms)}</td>
                      <td>{t.error && <div className="warn-text small">{t.error}</div>}<JsonView value={{ input: t.input, output: t.output, decision: t.decision }} collapsed label="I/O" /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ))}
        {tab === "models" && (f(d.model_calls).length === 0 ? <EmptyState title="No model calls" /> : (
          <div className="table-wrap">
            <table className="table table-compact">
              <thead><tr><th>Time</th><th>Agent</th><th>Purpose</th><th>Profile</th><th>Provider / model</th><th>Status</th><th className="num">Latency</th><th className="num">Tokens in/out</th><th className="num">Cost</th></tr></thead>
              <tbody>
                {f(d.model_calls).map((m) => (
                  <tr key={m.id} className={isJev(m) ? "row-jev" : undefined}>
                    <td className="small">{fmtTime(m.created_at)}</td>
                    <td>{m.agent_id ?? "—"}</td>
                    <td><code>{m.purpose}</code>{isJev(m) && <span className="tag tag-jev" title="TypeSafe Jev typed decision">JEV decision</span>}</td>
                    <td className="small">{m.profile}</td>
                    <td className="small">{m.provider} / {m.model}{m.attempt > 1 ? <span className="muted"> (attempt {m.attempt})</span> : null}</td>
                    <td><StatusBadge status={m.status} />{m.error && <div className="warn-text small clamp-2">{m.error}</div>}</td>
                    <td className="num">{fmtMs(m.latency_ms)}</td>
                    <td className="num"><Value value={m.input_tokens} format="int" /> / <Value value={m.output_tokens} format="int" /></td>
                    <td className="num"><Value value={m.cost_usd} format="usd" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        {tab === "queries" && (d.queries.length === 0 ? <EmptyState title="No queries" /> : (
          <ul className="query-list">
            {d.queries.map((q) => (
              <li key={q.id} className="query-item">
                <div className="chip-row">
                  <code>{q.id}</code><StatusBadge status={q.status} /><span className="small">{q.actor}</span><span className="muted small">{q.purpose}</span>
                  <span className="small">{q.row_count} rows{q.truncated ? " (truncated)" : ""}</span><span className="small">{fmtMs(q.duration_ms)}</span>
                  {q.cache_hit && <span className="tag tag-info">cache hit</span>}
                </div>
                {q.rejected_reason && <p className="warn-text small"><strong>Gateway rejected:</strong> {q.rejected_reason}</p>}
                <details><summary className="small">SQL</summary><CodeBlock code={q.executed_sql ?? q.sql} /></details>
              </li>
            ))}
          </ul>
        ))}
      </Card>
    </div>
  );
}
