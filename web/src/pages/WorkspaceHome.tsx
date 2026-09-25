import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { to } from "../routes";
import { api, type Source, type WorkspaceDetail } from "../api";
import { Card, ConfidenceBar, EmptyState, ErrorBox, Field, KeyValue, Loading, PageHeader, Stat, StatusBadge, Value } from "../components/ui";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { AUTONOMY_LEVELS, TERMINAL_RUN } from "../lib/status";

export function WorkspaceHomePage() {
  const { wsId = "" } = useParams();
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);
  const insights = useAsync(() => api.listInsights(wsId), [wsId]);
  const artifacts = useAsync(() => api.listArtifacts(wsId), [wsId]);

  if (ws.error) return <div className="page"><ErrorBox error={ws.error} onRetry={ws.reload} /></div>;
  if (!ws.data) return <div className="page"><Loading /></div>;
  const w = ws.data;
  const verified = (insights.data ?? []).filter((i) => i.status === "verified").slice(0, 5);
  const level = AUTONOMY_LEVELS[w.autonomy_level];

  return (
    <div className="page">
      <PageHeader title={w.name} subtitle={w.description || undefined}
        actions={<span className="tag tag-info" title={level?.description}>Autonomy L{w.autonomy_level} · {level?.name}</span>} />

      <WhatChanged wsId={wsId} verifiedFindings={insights.error || !insights.data ? undefined : insights.data.filter((i) => i.status === "verified").length}
        runningRuns={runs.error ? undefined : runs.data?.filter((r) => !TERMINAL_RUN.has(r.status)).length} />

      <div className="grid-2">
        <Card title="Objective">
          <p>{w.objective || <span className="muted">No objective set.</span>}</p>
          <div className="stats-row">
            <Stat label="Sources" value={<Value value={w.counts?.sources} format="int" />} />
            <Stat label="Runs" value={<Value value={w.counts?.runs} format="int" />} />
            <Stat label="Verified insights" value={<Value value={w.counts?.verified_insights} format="int" />} />
            <Stat label="Datasets" value={<Value value={w.counts?.dataset} format="int" />} />
            <Stat label="Metrics" value={<Value value={w.counts?.metric} format="int" />} />
            <Stat label="Charts" value={<Value value={w.counts?.chart} format="int" />} />
            <Stat label="Dashboards" value={<Value value={w.counts?.dashboard} format="int" />} />
          </div>
        </Card>
        <StartAnalysis ws={w} sources={sources.data ?? []} />
      </div>

      <div className="grid-2">
        <Card title="Investigations" actions={<Link to={to.investigations(wsId)} className="btn btn-sm btn-ghost">All investigations</Link>}>
          <ErrorBox error={runs.error} />
          {runs.data?.length === 0 && <EmptyState title="No runs yet">Start an analysis once a source has selected tables.</EmptyState>}
          <ul className="list">
            {runs.data?.slice(0, 6).map((r) => (
              <li key={r.id} className="list-item">
                <Link to={to.run(wsId, r.id)} className="list-main">
                  <span className="clamp-1">{r.objective}</span>
                  <span className="muted small">{fmtDate(r.created_at)} · <Value value={r.tokens} format="int" suffix="tokens" /> · <Value value={r.cost_usd} format="usd" /></span>
                </Link>
                <StatusBadge status={r.status} />
              </li>
            ))}
          </ul>
        </Card>
        <Card title="Key verified insights" actions={<Link to={to.findings(wsId)} className="btn btn-sm btn-ghost">All insights</Link>}>
          <ErrorBox error={insights.error} />
          {insights.data && verified.length === 0 && <EmptyState title="No verified insights yet" />}
          <ul className="list">
            {verified.map((i) => (
              <li key={i.id} className="list-item">
                <Link to={to.findings(wsId, i.id)} className="list-main">
                  <span><strong>{i.code}</strong> {i.title}</span>
                  <span className="muted small clamp-2">{i.finding}</span>
                </Link>
                <div style={{ minWidth: 110 }}><ConfidenceBar value={i.confidence} /></div>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <div className="grid-3">
        <Card title="Sources" actions={<Link to={to.sources(wsId)} className="btn btn-sm btn-ghost">Manage</Link>}>
          <ErrorBox error={sources.error} />
          {sources.data?.length === 0 && <EmptyState title="No sources">Add ServiceNow, PostgreSQL or a CSV upload.</EmptyState>}
          <ul className="list">
            {sources.data?.map((s) => (
              <li key={s.id} className="list-item">
                <div className="list-main"><span>{s.name}</span><span className="muted small">{s.kind} · {s.execution_mode}</span></div>
                <StatusBadge status={s.status} />
              </li>
            ))}
          </ul>
        </Card>
        <Card title="Recent artifacts" actions={<Link to={to.studio(wsId)} className="btn btn-sm btn-ghost">Studio</Link>}>
          <ErrorBox error={artifacts.error} />
          {artifacts.data?.length === 0 && <EmptyState title="No artifacts yet" />}
          <ul className="list">
            {artifacts.data?.slice(0, 8).map((a) => (
              <li key={a.id} className="list-item">
                <Link to={to.studio(wsId, a.id)} className="list-main">
                  <span className="clamp-1">{a.name}</span><span className="muted small">{a.type} · v{a.version}</span>
                </Link>
                <StatusBadge status={a.status} />
              </li>
            ))}
          </ul>
        </Card>
        <Card title="Members & policy" actions={<Link to={to.governance(wsId)} className="btn btn-sm btn-ghost">Edit</Link>}>
          <p className="small">Your role: <strong>{w.role}</strong> · policy v{w.policy_version}</p>
          <ul className="list compact">
            {w.members.map((m) => (
              <li key={m.user_id} className="list-item"><span>{m.name || m.email}</span><span className="tag">{m.role}</span></li>
            ))}
          </ul>
          <KeyValue items={[
            ["Max rows", <Value key="mr" value={w.policy.max_rows} format="int" />],
            ["PII access", w.policy.pii_access],
            ["Publish to", (w.policy.publish_destinations ?? []).join(", ") || "—"],
            ["Publish approval", w.policy.publish_requires_approval ? "required" : "not required"],
            ["Run budget", <span key="rb"><Value value={w.policy.run_cost_budget_usd} format="usd" /> / <Value value={w.policy.run_token_budget} format="int" suffix="tokens" /></span>],
            ["Restricted columns", String((w.policy.restricted_columns ?? []).length)],
          ]} />
        </Card>
      </div>
    </div>
  );
}

/**
 * Home: what needs attention. Each count is fetched on its own; a count that could not be loaded
 * shows as unknown, never as 0 ("no open alerts" and "could not check alerts" are different facts).
 */
function WhatChanged({ wsId, verifiedFindings, runningRuns }: { wsId: string; verifiedFindings?: number; runningRuns?: number }) {
  const alerts = useAsync(() => api.listAlerts(wsId, "open"), [wsId]);
  const approvals = useAsync(() => api.listApprovals(wsId, "pending"), [wsId]);
  const count = (s: { data?: unknown[]; error: string | null }) => (s.error || !s.data ? undefined : s.data.length);
  return (
    <Card title="What changed">
      <div className="stats-row">
        <Link to={to.monitoring(wsId, { tab: "alerts" })} className="stat-link">
          <Stat label="Open alerts" value={<Value value={count(alerts)} format="int" unknownTitle={alerts.error ?? "Loading"} />} />
        </Link>
        <Link to={to.approvals(wsId)} className="stat-link">
          <Stat label="Pending approvals" value={<Value value={count(approvals)} format="int" unknownTitle={approvals.error ?? "Loading"} />} />
        </Link>
        <Link to={to.findings(wsId)} className="stat-link">
          <Stat label="Verified findings" value={<Value value={verifiedFindings} format="int" />} />
        </Link>
        <Link to={to.investigations(wsId)} className="stat-link">
          <Stat label="Investigations in progress" value={<Value value={runningRuns} format="int" />} />
        </Link>
      </div>
    </Card>
  );
}

function StartAnalysis({ ws, sources }: { ws: WorkspaceDetail; sources: Source[] }) {
  const nav = useNavigate();
  const [objective, setObjective] = useState(ws.objective);
  const [sourceId, setSourceId] = useState("");
  const [level, setLevel] = useState<string>("");
  const act = useAction();
  useEffect(() => setObjective(ws.objective), [ws.objective]);
  const ready = sources.filter((s) => s.status === "ready");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const run = await act.run(() => api.startRun(ws.id, {
      objective: objective.trim() || undefined,
      source_ids: sourceId ? [sourceId] : undefined,
      autonomy_level: level === "" ? undefined : Number(level),
    }));
    if (run) nav(to.run(ws.id, run.id));
  };

  return (
    <Card title="Start analysis">
      <form className="form" onSubmit={submit}>
        <Field label="Objective" htmlFor="run-objective" hint="What should the agents find out? At least 10 characters.">
          <textarea id="run-objective" rows={4} value={objective} onChange={(e) => setObjective(e.target.value)} />
        </Field>
        <div className="form-row">
          <Field label="Source" htmlFor="run-source" hint={ready.length > 1 ? "One source per run in this release." : undefined}>
            <select id="run-source" value={sourceId} onChange={(e) => setSourceId(e.target.value)}>
              <option value="">{ready.length > 1 ? "Choose a source…" : "All selected assets"}</option>
              {ready.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.kind})</option>)}
            </select>
          </Field>
          <Field label="Autonomy for this run" htmlFor="run-level">
            <select id="run-level" value={level} onChange={(e) => setLevel(e.target.value)}>
              <option value="">Workspace default (L{ws.autonomy_level})</option>
              {AUTONOMY_LEVELS.map((l) => <option key={l.level} value={l.level}>L{l.level} · {l.name}</option>)}
            </select>
          </Field>
        </div>
        {ready.length === 0 && <p className="muted small">No source is ready yet: add one, discover it and select tables first.</p>}
        <ErrorBox error={act.error} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Starting…" : "Start analysis"}</button>
        </div>
      </form>
    </Card>
  );
}
