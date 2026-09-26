import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { to } from "../routes";
import { api } from "../api";
import { AutonomyPicker } from "../components/AutonomyPicker";
import { Card, EmptyState, ErrorBox, Field, Loading, PageHeader, Value } from "../components/ui";
import { useAction, useAsync } from "../lib/hooks";
import { AUTONOMY_LEVELS } from "../lib/status";
import { fmtDate } from "../lib/format";

export function WorkspacesPage() {
  const list = useAsync(() => api.listWorkspaces(), []);
  const [showCreate, setShowCreate] = useState(false);
  const statusAction = useAction();
  const changeStatus = async (id: string, name: string, status: "active" | "disabled") => {
    if (status === "disabled" && !window.confirm(`Disable ${name}? New work, scheduled runs and workspace access will stop. Data is retained.`)) return;
    const result = await statusAction.run(() => api.setWorkspaceStatus(id, status));
    if (result) await list.reload();
  };
  return (
    <div className="page">
      <PageHeader title="Workspaces" subtitle="Each workspace holds sources, a policy, members, analysis runs and their evidence."
        actions={<button type="button" className="btn btn-primary" onClick={() => setShowCreate((s) => !s)}>
          {showCreate ? "Close" : "New workspace"}</button>} />
      {showCreate && <CreateWorkspace />}
      <ErrorBox error={list.error} onRetry={list.reload} />
      <ErrorBox error={statusAction.error} />
      {list.loading && !list.data ? <Loading /> : list.data && list.data.length === 0 ? (
        <EmptyState title="No workspaces yet">Create one to connect a source and start an analysis.</EmptyState>
      ) : (
        <div className="grid-cards">
          {list.data?.map((w) => (
            <div key={w.id} className="card">
              <div className="card-body">
                <h2 className="card-title">{w.status === "active" ? <Link to={to.workspace(w.id)}>{w.name}</Link> : w.name}</h2>
                {w.status !== "active" && <span className="tag tag-warning">disabled</span>}
                {w.description && <p className="muted clamp-2">{w.description}</p>}
                {w.objective && <p className="small clamp-2"><strong>Objective:</strong> {w.objective}</p>}
                <div className="chip-row small">
                  <span className="tag tag-info">L{w.autonomy_level} · {AUTONOMY_LEVELS[w.autonomy_level]?.name ?? "?"}</span>
                  <span className="tag"><Value value={w.counts?.sources} format="int" suffix="sources" unknownLabel="sources unknown" /></span>
                  <span className="tag"><Value value={w.counts?.runs} format="int" suffix="runs" unknownLabel="runs unknown" /></span>
                  <span className="tag tag-success"><Value value={w.counts?.verified_insights} format="int" suffix="verified insights" unknownLabel="verified insights unknown" /></span>
                  <span className="tag"><Value value={w.counts?.dashboard} format="int" suffix="dashboards" unknownLabel="dashboards unknown" /></span>
                </div>
                <p className="muted small">Created {fmtDate(w.created_at)}</p>
                {w.role === "owner" && <button type="button" className="btn btn-sm" disabled={statusAction.busy}
                  onClick={() => void changeStatus(w.id, w.name, w.status === "active" ? "disabled" : "active")}>
                  {w.status === "active" ? "Disable workspace" : "Reactivate workspace"}
                </button>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CreateWorkspace() {
  const nav = useNavigate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [objective, setObjective] = useState("");
  const [level, setLevel] = useState(3);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const ws = await act.run(() => api.createWorkspace({ name: name.trim(), description, objective, autonomy_level: level }));
    if (ws) nav(to.workspace(ws.id));
  };
  return (
    <Card title="Create workspace">
      <form onSubmit={submit} className="form">
        <Field label="Name" htmlFor="ws-name">
          <input id="ws-name" required value={name} onChange={(e) => setName(e.target.value)} placeholder="IT Service Management" />
        </Field>
        <Field label="Description" htmlFor="ws-desc">
          <input id="ws-desc" value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label="Business objective" htmlFor="ws-obj" hint="Pre-fills new analysis runs. At least 10 characters to start a run.">
          <textarea id="ws-obj" rows={3} value={objective} onChange={(e) => setObjective(e.target.value)}
            placeholder="Why are incident resolution times increasing, and which groups drive SLA breaches?" />
        </Field>
        <AutonomyPicker value={level} onChange={setLevel} />
        <ErrorBox error={act.error} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={act.busy || !name.trim()}>{act.busy ? "Creating…" : "Create workspace"}</button>
        </div>
      </form>
    </Card>
  );
}
