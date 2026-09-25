import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { to } from "../routes";
import { api, subscribeRunEvents, type FeedbackResponse, type Publication, type RunEvent, type RunOrigin, type RunTask } from "../api";
import { ApprovalsPanel } from "../components/ApprovalsPanel";
import { ChangesPanel } from "../components/ChangesPanel";
import { InvestigationTree } from "../components/InvestigationTree";
import { Markdown } from "../components/Markdown";
import { Card, EmptyState, ErrorBox, Field, JsonView, KeyValue, Loading, Notice, PageHeader, StatusBadge, Tabs, Tag, Value } from "../components/ui";
import { durationBetween, fmtDate, fmtPct, fmtTime, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { TERMINAL_RUN } from "../lib/status";

const FEEDBACK_KINDS = [
  { id: "", label: "Auto-classify (JEV)" },
  { id: "redirect", label: "Redirect — change focus / filters" },
  { id: "add_context", label: "Add context — business definitions" },
  { id: "reject_finding", label: "Reject finding" },
  { id: "deeper_analysis", label: "Deeper analysis" },
];

type StreamState = "connecting" | "open" | "reconnecting" | "closed";

export function RunViewPage() {
  const { wsId = "", runId = "" } = useParams();
  const run = useAsync(() => api.getRun(wsId, runId), [wsId, runId]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [stream, setStream] = useState<{ state: StreamState; error?: string }>({ state: "connecting" });
  const [tab, setTab] = useState<"tree" | "tasks" | "events">("tree");
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const control = useAction();
  const reload = run.reload;

  // Coalesce bursts of events into one refetch of the run detail.
  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) return;
    refreshTimer.current = setTimeout(() => {
      refreshTimer.current = null;
      void reload();
    }, 800);
  }, [reload]);

  useEffect(() => {
    setEvents([]);
    const h = subscribeRunEvents(wsId, runId, {
      onEvent: (ev) => {
        setEvents((prev) => (prev.length > 500 ? [...prev.slice(-400), ev] : [...prev, ev]));
        scheduleRefresh();
      },
      onEnd: () => scheduleRefresh(),
      onStatus: (state, error) => setStream({ state, error }),
    });
    return () => {
      h.close();
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    };
  }, [wsId, runId, scheduleRefresh]);

  if (run.error && !run.data) return <div className="page"><ErrorBox error={run.error} onRetry={run.reload} /></div>;
  if (!run.data) return <div className="page"><Loading label="Loading run…" /></div>;
  const r = run.data;
  const terminal = TERMINAL_RUN.has(r.status);
  const doControl = async (action: "pause" | "resume" | "cancel") => {
    if (action === "cancel" && !window.confirm("Cancel this run? Work in progress stops; nothing unapproved is published.")) return;
    const res = await control.run(() => api.controlRun(wsId, runId, action));
    if (res) void run.reload();
  };
  const pending = r.approvals.filter((a) => a.status === "pending");
  const pub = r.summary?.publication;

  return (
    <div className="page">
      <PageHeader
        title={<span className="run-title">{r.objective}</span>}
        subtitle={<>
          <StatusBadge status={r.status} /> <OriginBadge wsId={wsId} origin={r.origin} /> <span className="muted small">run <code>{r.id}</code> · L{r.autonomy_level} · plan v{r.plan_version}
            {r.plan_hash ? <> (<code title={r.plan_hash}>{shortHash(r.plan_hash, 10)}</code>)</> : null} · iteration {r.iteration}</span>
        </>}
        actions={<>
          <StreamIndicator state={stream.state} error={stream.error} />
          <button type="button" className="btn btn-sm" onClick={() => doControl("pause")} disabled={terminal || r.status === "PAUSED" || control.busy}>Pause</button>
          <button type="button" className="btn btn-sm" onClick={() => doControl("resume")} disabled={terminal || r.status !== "PAUSED" || control.busy}>Resume</button>
          <button type="button" className="btn btn-sm btn-danger" onClick={() => doControl("cancel")} disabled={terminal || control.busy}>Cancel</button>
          <Link to={to.runConsole(wsId, runId)} className="btn btn-sm btn-ghost">Agent console</Link>
        </>}
      />
      <ErrorBox error={control.error} />
      {r.error && <Notice tone="danger"><strong>Run error:</strong> {r.error}</Notice>}
      {pending.length > 0 && <Notice tone="warning">{pending.length} approval{pending.length > 1 ? "s" : ""} waiting — see the Approvals panel.</Notice>}

      <div className="stats-row card card-body">
        <KeyValue items={[
          ["Started", fmtDate(r.started_at ?? r.created_at)],
          ["Duration", durationBetween(r.started_at, r.finished_at)],
          ["Tokens", <Value key="t" value={r.tokens} format="int" />],
          ["Cost", <Value key="c" value={r.cost_usd} format="usd" />],
          ["Tasks", `${r.tasks.filter((t) => t.status === "COMPLETED").length}/${r.tasks.length} done`],
          ["Hypotheses", String(r.hypotheses.length)],
          ["Findings", `${r.insights.filter((i) => i.verified).length} verified / ${r.insights.length}`],
        ]} />
      </div>

      {r.summary?.changes && <ChangesPanel changes={r.summary.changes} wsId={wsId} reportArtifactId={r.summary.report_artifact_id} />}
      {!r.summary?.changes && r.summary?.report_artifact_id && (
        <Notice tone="info">A report was generated for this run: <Link to={to.reports(wsId, r.summary.report_artifact_id)}>open in Reports</Link>.</Notice>
      )}

      {(r.summary?.summary_markdown || pub) && (
        <Card title="Result summary" actions={r.summary?.summary_source ? <span className="muted small">source: {r.summary.summary_source}</span> : undefined}>
          <Markdown text={r.summary?.summary_markdown} />
          {r.summary?.cancel_outcome && <p className="muted small">Cancel outcome: {r.summary.cancel_outcome}</p>}
          {pub && <PublicationBox pub={pub} onRolledBack={run.reload} />}
        </Card>
      )}

      <div className="run-grid">
        <div className="run-main">
          <Tabs value={tab} onChange={setTab} tabs={[
            { id: "tree", label: `Investigation (${r.hypotheses.length})` },
            { id: "tasks", label: `Plan & tasks (${r.tasks.length})` },
            { id: "events", label: `Live events (${events.length})` },
          ]} />
          <div className="tab-panel card card-body" role="tabpanel">
            {tab === "tree" && <InvestigationTree wsId={wsId} objective={r.objective} hypotheses={r.hypotheses} insights={r.insights} />}
            {tab === "tasks" && <TaskBoard tasks={r.tasks} />}
            {tab === "events" && <EventFeed events={events} />}
          </div>
        </div>
        <aside className="run-side">
          <Card title={`Approvals${pending.length ? ` (${pending.length} pending)` : ""}`}>
            <ApprovalsPanel approvals={r.approvals} onDecided={() => void run.reload()} />
          </Card>
          <FeedbackBox wsId={wsId} runId={runId} insights={r.insights.map((i) => ({ id: i.id, label: `${i.code} ${i.title}` }))}
            disabled={["FAILED", "CANCELLED", "REJECTED"].includes(r.status)} onDone={() => void run.reload()} />
          {(r.instructions?.length ?? 0) > 0 && (
            <Card title="User instructions">
              <ol className="small">
                {r.instructions.map((ins, i) => <li key={i}><strong>{String(ins.kind)}</strong>: {String(ins.text)}</li>)}
              </ol>
              {r.constraints && Object.keys(r.constraints).length > 0 && <JsonView value={r.constraints} collapsed label="Compiled constraints" />}
            </Card>
          )}
          <Card title="Authorized scope">
            <KeyValue items={[
              ["Assets", ((r.scope.assets as string[] | undefined) ?? []).join(", ") || "—"],
              ["Denied columns", String(((r.scope.denied_columns as string[] | undefined) ?? []).length)],
              ["Max rows", String(r.scope.max_rows ?? "—")],
              ["Policy version", String(r.scope.policy_version ?? r.policy_version)],
            ]} />
          </Card>
        </aside>
      </div>
    </div>
  );
}

/** Where a run came from: a schedule firing or an alert investigation (user runs show nothing). */
export function OriginBadge({ wsId, origin }: { wsId: string; origin: RunOrigin | null | undefined }) {
  if (!origin || !origin.type || origin.type === "user") return null;
  if (origin.type === "schedule") {
    return (
      <span className="origin">
        <Tag tone="info">scheduled</Tag>{" "}
        {origin.schedule_id && <Link className="small" to={to.schedules(wsId, origin.schedule_id)}>schedule</Link>}
        {origin.previous_run_id && <> · <Link className="small" to={to.run(wsId, origin.previous_run_id)}>previous run</Link></>}
        {origin.publish && <span className="muted small"> · publish: {origin.publish}</span>}
      </span>
    );
  }
  if (origin.type === "alert") {
    return (
      <span className="origin">
        <Tag tone="warning">{origin.automatic ? "alert investigation (automatic)" : "alert investigation"}</Tag>{" "}
        {origin.alert_id && <Link className="small" to={to.monitoring(wsId, { tab: "alerts", alert: origin.alert_id })}>alert</Link>}
      </span>
    );
  }
  return <Tag>{origin.type}</Tag>;
}

function StreamIndicator({ state, error }: { state: StreamState; error?: string }) {
  const label = state === "open" ? "Live" : state === "connecting" ? "Connecting" : state === "reconnecting" ? "Reconnecting" : "Stream closed";
  return (
    <span className={`stream stream-${state}`} role="status" title={error ?? label}>
      <span className="stream-dot" aria-hidden="true" />{label}
    </span>
  );
}

function PublicationBox({ pub, onRolledBack }: { pub: Publication; onRolledBack: () => void }) {
  const act = useAction();
  const [done, setDone] = useState<string | null>(null);
  const urls = Object.entries(pub.urls ?? {});
  const rollback = async () => {
    if (!pub.publication_id || !window.confirm("Roll back this publication? Published dashboards are removed from the destination.")) return;
    const r = await act.run(() => api.rollback(pub.publication_id!));
    if (r) {
      setDone("Publication rolled back.");
      onRolledBack();
    }
  };
  return (
    <div className="publication">
      <h3>Publication <StatusBadge status={pub.status} /></h3>
      {urls.length === 0 ? <p className="muted small">No URLs returned.</p> : (
        <ul>{urls.map(([k, u]) => <li key={k}><a href={u} target="_blank" rel="noreferrer noopener">{k}</a> <span className="muted small">{u}</span></li>)}</ul>
      )}
      {pub.publication_id && (
        <button type="button" className="btn btn-sm btn-danger" onClick={rollback} disabled={act.busy}>{act.busy ? "Rolling back…" : "Roll back publication"}</button>
      )}
      <ErrorBox error={act.error} />
      {done && <Notice tone="success">{done}</Notice>}
    </div>
  );
}

function TaskBoard({ tasks }: { tasks: RunTask[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!tasks.length) return <EmptyState title="No plan yet">The supervisor is planning.</EmptyState>;
  const sorted = [...tasks].sort((a, b) => a.seq - b.seq || a.key.localeCompare(b.key));
  const counts = sorted.reduce<Record<string, number>>((acc, t) => ({ ...acc, [t.status]: (acc[t.status] ?? 0) + 1 }), {});
  return (
    <>
      <div className="chip-row">{Object.entries(counts).map(([s, n]) => <StatusBadge key={s} status={s} label={`${s} · ${n}`} />)}</div>
      <div className="table-wrap">
        <table className="table table-compact">
          <thead><tr><th className="num">#</th><th>Task</th><th>Agent</th><th>Status</th><th className="num">Attempts</th><th>Duration</th><th>Error</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>
            {sorted.map((t) => (
              <Fragment key={t.id}>
                <tr>
                  <td className="num">{t.seq}</td>
                  <td><code>{t.key}</code><div className="muted small">{t.title}</div></td>
                  <td>{t.agent_id}</td>
                  <td><StatusBadge status={t.status} /></td>
                  <td className="num">{t.attempts}</td>
                  <td className="small">{durationBetween(t.started_at, t.finished_at)}</td>
                  <td className="small warn-text clamp-2">{t.error ?? ""}</td>
                  <td><button type="button" className="btn btn-xs btn-ghost" aria-expanded={open === t.id} onClick={() => setOpen(open === t.id ? null : t.id)}>
                    {open === t.id ? "Hide" : "Details"}</button></td>
                </tr>
                {open === t.id && (
                  <tr className="row-detail"><td colSpan={8}><TaskDetail task={t} /></td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function TaskDetail({ task }: { task: RunTask }) {
  const detail = useAsync(() => api.agentRun(task.id), [task.id]);
  return (
    <div className="task-detail">
      <p className="small muted">Depends on: {task.depends_on.join(", ") || "—"} · plan v{task.plan_version}</p>
      <div className="grid-2">
        <JsonView value={task.input} collapsed label="Input" />
        <JsonView value={task.output} collapsed label="Output" />
      </div>
      <ErrorBox error={detail.error} />
      {detail.data && (
        <>
          <p className="small">
            {detail.data.messages.length} messages · {detail.data.tool_calls.length} tool calls · {detail.data.model_calls.length} model calls ·{" "}
            {detail.data.queries.length} queries
          </p>
          <ul className="timeline small">
            {detail.data.messages.map((m) => <li key={m.id}><span className="muted">{fmtTime(m.created_at)} [{m.kind}]</span> {m.content}</li>)}
          </ul>
        </>
      )}
    </div>
  );
}

function describeEvent(ev: RunEvent): string {
  const p = ev.payload ?? {};
  const pick = (...keys: string[]) => keys.map((k) => p[k]).find((v) => v !== undefined && v !== null && v !== "");
  const main = pick("content", "message", "text", "title", "statement", "code", "key", "status", "reason", "summary");
  return main !== undefined ? String(main) : "";
}

function EventFeed({ events }: { events: RunEvent[] }) {
  if (!events.length) return <EmptyState title="Waiting for events…" />;
  const shown = [...events].reverse().slice(0, 300);
  return (
    <ol className="events" aria-live="polite" aria-relevant="additions">
      {shown.map((ev) => (
        <li key={ev.id} className="event">
          <span className="event-time muted small">{fmtTime(ev.created_at)}</span>
          <span className={`event-type type-${ev.type.split(".")[0]}`}>{ev.type}</span>
          <span className="event-text">{describeEvent(ev)}</span>
          {ev.actor && <span className="muted small">{ev.actor}</span>}
          <details className="event-payload"><summary className="small">payload</summary><JsonView value={ev.payload} /></details>
        </li>
      ))}
    </ol>
  );
}

function FeedbackBox({ wsId, runId, insights, disabled, onDone }: {
  wsId: string; runId: string; insights: { id: string; label: string }[]; disabled: boolean; onDone: () => void;
}) {
  const [text, setText] = useState("");
  const [kind, setKind] = useState("");
  const [target, setTarget] = useState("");
  const [resp, setResp] = useState<FeedbackResponse | null>(null);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.feedback(wsId, runId, {
      text: text.trim(),
      kind: kind || null,
      target_type: kind === "reject_finding" ? "insight" : null,
      target_id: kind === "reject_finding" ? target || null : null,
    }));
    if (r) {
      setResp(r);
      setText("");
      onDone();
    }
  };
  return (
    <Card title="Feedback & redirect">
      <form className="form" onSubmit={submit}>
        <Field label="Instruction" htmlFor="fb-text">
          <textarea id="fb-text" rows={3} value={text} onChange={(e) => setText(e.target.value)} required
            placeholder="Focus on priority 1 incidents in the Network group; exclude auto-closed tickets." disabled={disabled} />
        </Field>
        <Field label="Kind" htmlFor="fb-kind">
          <select id="fb-kind" value={kind} onChange={(e) => setKind(e.target.value)} disabled={disabled}>
            {FEEDBACK_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
        </Field>
        {kind === "reject_finding" && (
          <Field label="Finding" htmlFor="fb-target">
            <select id="fb-target" value={target} onChange={(e) => setTarget(e.target.value)} required>
              <option value="">Choose a finding…</option>
              {insights.map((i) => <option key={i.id} value={i.id}>{i.label}</option>)}
            </select>
          </Field>
        )}
        <ErrorBox error={act.error} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || disabled || !text.trim()}>{act.busy ? "Sending…" : "Send feedback"}</button>
        </div>
      </form>
      {resp && (
        <div className="feedback-result">
          <KeyValue items={[
            ["Classified as", <strong key="k">{resp.kind}</strong>],
            ["Classified by", resp.classified_by],
            ["Consequential p", resp.consequential_p === null ? "—" : fmtPct(resp.consequential_p)],
          ]} />
          {resp.note && <Notice tone="warning">{resp.note}</Notice>}
          {resp.interpretation && (
            <div className="small">
              <p><strong>Interpretation</strong> <span className="muted">({resp.interpretation.interpreted_by})</span>: {resp.interpretation.summary}</p>
              {resp.interpretation.filters.length > 0 ? (
                <ul>{resp.interpretation.filters.map((f, i) => <li key={i}><code>{String(f.asset ?? "")}.{String(f.column)} {String(f.op)} {JSON.stringify(f.value)}</code></li>)}</ul>
              ) : <p className="muted">No structured filters (unmatched columns are dropped against the run scope).</p>}
            </div>
          )}
          {resp.replan && (
            <p className="small">Replanned to <strong>plan v{resp.replan.plan_version}</strong>; tasks reset: {JSON.stringify(resp.replan.tasks_reset)};
              approvals invalidated: {JSON.stringify(resp.replan.approvals_invalidated)}.</p>
          )}
        </div>
      )}
    </Card>
  );
}
