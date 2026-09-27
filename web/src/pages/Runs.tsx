import { useCallback } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type MlExperiment } from "../api";
import { useAuth } from "../auth";
import { BuildPanel } from "../components/BuildPanel";
import { PreparePanel } from "../components/PreparePanel";
import { StartWorkButton } from "../components/StartWork";
import { EmptyState, ErrorBox, KeyValue, Loading, PageHeader, StatusBadge, Tabs, Tag, Value } from "../components/ui";
import { durationBetween, fmtDate } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { to, type WorkTab } from "../routes";

const TABS: { id: WorkTab; label: string }[] = [
  { id: "investigations", label: "Investigations" }, { id: "prepare", label: "Prepare data" }, { id: "builds", label: "dbt builds" },
  { id: "ml", label: "ML experiments" },
];

/**
 * Work (spec v4 §15): everything started with Start work. Investigations, and the data-engineering
 * panels (Prepare data, dbt builds) that arrive as job kinds rather than new screens. Ask has its own
 * entry because it is the quick box.
 */
export function WorkPage() {
  const { wsId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = (TABS.some((t) => t.id === params.get("tab")) ? params.get("tab") : "investigations") as WorkTab;
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const { user } = useAuth();
  const set = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      return next;
    });
  }, [setParams]);
  const selectJob = useCallback((id: string | null) => set({ job: id }), [set]);
  return (
    <div className="page">
      <PageHeader title="Work" subtitle={<>Investigations and prepared data. For a quick question, use <Link to={to.ask(wsId)}>Ask</Link>.</>}
        actions={<StartWorkButton wsId={wsId} />} />
      <Tabs value={tab} onChange={(t) => setParams(t === "investigations" ? {} : { tab: t })} tabs={TABS} />
      <div className="tab-panel" role="tabpanel">
        {tab === "investigations" && <Investigations wsId={wsId} />}
        {tab === "prepare" && <PreparePanel wsId={wsId} role={ws.data?.role} recipe={params.get("recipe")}
          onSelectRecipe={(id) => set({ recipe: id })} />}
        {tab === "builds" && <BuildPanel wsId={wsId} selected={params.get("job")} onSelect={selectJob}
          canDesignate={!!user?.is_admin || ws.data?.role === "owner"} />}
        {tab === "ml" && <MLExperiments wsId={wsId} selected={params.get("experiment")}
          onSelect={(id) => set({ experiment: id })} />}
      </div>
    </div>
  );
}

function MLExperiments({ wsId, selected, onSelect }: { wsId: string; selected: string | null; onSelect: (id: string | null) => void }) {
  const rows = useAsync(() => api.listMlExperiments(wsId), [wsId]);
  const detail = useAsync(() => selected ? api.getMlExperiment(wsId, selected) : Promise.resolve(null), [wsId, selected]);
  return <>
    <ErrorBox error={rows.error} onRetry={rows.reload} />
    {rows.loading && !rows.data && <Loading />}
    {rows.data?.length === 0 && <EmptyState title="No ML experiments yet">Publish a model plan in Data → Definitions, then choose Forecast or Predict in Start work.</EmptyState>}
    {!!rows.data?.length && <div className="table-wrap card">
      <table className="table"><caption className="sr-only">ML experiments</caption>
        <thead><tr><th>Plan</th><th>Task</th><th>Dataset</th><th>Status</th><th>Started</th><th><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>{rows.data.map((r: MlExperiment) => <tr key={r.id}>
          <td>{r.definition_key} v{r.definition_version}</td><td>{r.task}</td><td>{r.dataset_asset}</td>
          <td><StatusBadge status={r.status} /></td><td className="small">{fmtDate(r.created_at)}</td>
          <td><button type="button" className="btn btn-xs btn-ghost" onClick={() => onSelect(r.id)}>View result<span className="sr-only"> for {r.definition_key} v{r.definition_version}</span></button></td>
        </tr>)}</tbody>
      </table>
    </div>}
    {selected && <section className="card" aria-label="ML experiment result">
      <button type="button" className="btn btn-ghost" onClick={() => onSelect(null)}>Close result</button>
      <ErrorBox error={detail.error} onRetry={detail.reload} />
      {detail.loading && !detail.data && <Loading />}
      {detail.data && <>
        <h2>{detail.data.definition_key} v{detail.data.definition_version}</h2>
        <p><StatusBadge status={detail.data.status} /> {detail.data.verdict && <span>{detail.data.verdict}</span>}</p>
        {detail.data.error && <p className="warn-text" role="status">{detail.data.error}</p>}
        <p>Task: {detail.data.task} · Dataset: {detail.data.dataset_asset}</p>
        {Object.keys(detail.data.summary ?? {}).length > 0 && <KeyValue items={[
          ["Metric", String(detail.data.summary.metric ?? "not evaluated")],
          ["Candidate", String(detail.data.summary.candidate ?? "—")],
          ["Baseline", String(detail.data.summary.baseline ?? "—")],
          ["Decision", String(detail.data.summary.decision ?? detail.data.verdict ?? "pending")],
          ["Estimator", String(detail.data.summary.estimator ?? "—")],
        ]} />}
        {Object.keys(detail.data.summary ?? {}).length > 0 && <details><summary>Full evaluation details</summary><pre>{JSON.stringify(detail.data.summary, null, 2)}</pre></details>}
        {Object.keys(detail.data.readiness ?? {}).length > 0 && <details><summary>Readiness checks</summary><pre>{JSON.stringify(detail.data.readiness, null, 2)}</pre></details>}
        {detail.data.verification && <details><summary>Verification</summary><pre>{JSON.stringify(detail.data.verification, null, 2)}</pre></details>}
      </>}
    </section>}
  </>;
}

function Investigations({ wsId }: { wsId: string }) {
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);
  return (
    <>
      <ErrorBox error={runs.error} onRetry={runs.reload} />
      {runs.loading && !runs.data && <Loading />}
      {runs.data?.length === 0 && <EmptyState title="No investigations yet">Use Start work once a source has selected tables.</EmptyState>}
      {!!runs.data?.length && (
        <div className="table-wrap card">
          <table className="table">
            <caption className="sr-only">Investigations</caption>
            <thead>
              <tr><th>Objective</th><th>Status</th><th>Started</th><th>Duration</th><th className="num">Cost</th><th><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id}>
                  <td><Link to={to.run(wsId, r.id)} className="clamp-2">{r.objective}</Link>
                    {r.origin?.type === "schedule" && <div className="small"><Tag tone="info">scheduled</Tag></div>}
                    {r.origin?.type === "alert" && <div className="small"><Tag tone="warning">from an alert</Tag></div>}</td>
                  <td><StatusBadge status={r.status} /></td>
                  <td className="small">{fmtDate(r.started_at ?? r.created_at)}</td>
                  <td className="small">{durationBetween(r.started_at, r.finished_at)}</td>
                  <td className="num"><Value value={r.cost_usd} format="usd" /></td>
                  <td><Link to={to.runConsole(wsId, r.id)} className="btn btn-xs btn-ghost">Agent console<span className="sr-only"> for {r.objective}</span></Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
