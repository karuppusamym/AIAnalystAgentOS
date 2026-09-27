import { useCallback } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { BuildPanel } from "../components/BuildPanel";
import { DataThreadPanel } from "../components/DataThread";
import { NotebooksPanel } from "../components/Notebooks";
import { PreparePanel } from "../components/PreparePanel";
import { StartWorkButton } from "../components/StartWork";
import { EmptyState, ErrorBox, Loading, PageHeader, StatusBadge, Tabs, Tag, Value } from "../components/ui";
import { durationBetween, fmtDate } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { to, type WorkTab } from "../routes";

const TABS: { id: WorkTab; label: string }[] = [
  { id: "investigations", label: "Investigations" }, { id: "thread", label: "Data Thread" }, { id: "notebooks", label: "Notebooks" },
  { id: "prepare", label: "Prepare data" }, { id: "builds", label: "dbt builds" },
];

/**
 * Work (spec v4 §15): everything started with Start work. Investigations and their Data Thread
 * (steps, branches, merge), notebooks (cells as steps), and the data-engineering panels (Prepare data, dbt builds) that arrive
 * as job kinds rather than new screens. Ask has its own entry because it is the quick box.
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
  // Platform admins act with owner authority in any workspace (the server decides either way).
  const role = user?.is_admin ? "owner" : ws.data?.role;
  return (
    <div className="page">
      <PageHeader title="Work" subtitle={<>Investigations and prepared data. For a quick question, use <Link to={to.ask(wsId)}>Ask</Link>.</>}
        actions={<StartWorkButton wsId={wsId} />} />
      <Tabs value={tab} onChange={(t) => setParams(t === "investigations" ? {} : { tab: t })} tabs={TABS} />
      <div className="tab-panel" role="tabpanel">
        {tab === "investigations" && <Investigations wsId={wsId} />}
        {tab === "thread" && <DataThreadPanel wsId={wsId} role={role} container={params.get("container")} branch={params.get("branch")}
          onChange={(patch) => set(patch as Record<string, string | null>)} />}
        {tab === "notebooks" && <NotebooksPanel wsId={wsId} role={role} selected={params.get("notebook")} onSelect={(id) => set({ notebook: id })} />}
        {tab === "prepare" && <PreparePanel wsId={wsId} role={ws.data?.role} recipe={params.get("recipe")}
          onSelectRecipe={(id) => set({ recipe: id })} />}
        {tab === "builds" && <BuildPanel wsId={wsId} selected={params.get("job")} onSelect={selectJob}
          canDesignate={!!user?.is_admin || ws.data?.role === "owner"} />}
      </div>
    </div>
  );
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
                  <td><Link to={to.thread(wsId, "run", r.id)} className="btn btn-xs btn-ghost">Data Thread<span className="sr-only"> of {r.objective}</span></Link>
                    <Link to={to.runConsole(wsId, r.id)} className="btn btn-xs btn-ghost">Agent console<span className="sr-only"> for {r.objective}</span></Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
