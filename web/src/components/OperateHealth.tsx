import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type Monitor, type MonitorConfig } from "../api";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { describeMlMonitor, ML_MONITOR_KINDS, monitorMessage } from "../lib/monitors";
import { roleAtLeast, to } from "../routes";
import { EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge, Tag } from "./ui";

/** Validation of the model-monitor form; each kind needs its own fields (services/monitors.py). */
export function mlMonitorProblems(kind: string, f: Record<string, string>): string[] {
  const p: string[] = [];
  if (!f.name?.trim()) p.push("Name the monitor.");
  if (!f.model) p.push("Choose a model.");
  const n = (k: string) => Number(f[k]);
  if (kind === "ml_drift" && !(n("psi") > 0)) p.push("The PSI threshold must be positive.");
  if (kind === "ml_freshness" && !(n("maxAge") > 0)) p.push("The maximum age must be a positive number of hours.");
  if (kind === "ml_performance") {
    if (!f.labelAsset?.trim() || !f.labelColumn?.trim()) p.push("Name the table and column where labels mature.");
    if (!(Number.isInteger(n("horizon")) && n("horizon") >= 1)) p.push("The label horizon is a whole number of days.");
  }
  return p;
}

export function mlMonitorConfig(kind: string, f: Record<string, string>): MonitorConfig {
  if (kind === "ml_drift") return { model: f.model, psi_threshold: Number(f.psi) } as MonitorConfig;
  if (kind === "ml_freshness") return { model: f.model, max_age_hours: Number(f.maxAge) } as MonitorConfig;
  return { model: f.model, label_asset: f.labelAsset.trim(), label_column: f.labelColumn.trim(), label_horizon_days: Number(f.horizon),
    tolerance: Number(f.tolerance || 0.05), min_labels: Number(f.minLabels || 30) } as MonitorConfig;
}

function WatchModelForm({ wsId, models, onSaved }: { wsId: string; models: string[]; onSaved: () => void }) {
  const id = useId();
  const [kind, setKind] = useState("ml_drift");
  const [f, setF] = useState<Record<string, string>>({ name: "", model: models[0] ?? "", psi: "0.2", maxAge: "48", labelAsset: "", labelColumn: "", horizon: "14",
    tolerance: "0.05", minLabels: "30" });
  const [tried, setTried] = useState(false);
  const act = useAction();
  const set = (k: string, v: string) => setF((x) => ({ ...x, [k]: v }));
  const problems = mlMonitorProblems(kind, f);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTried(true);
    if (problems.length) return;
    const r = await act.run(() => api.createMonitor(wsId, { name: f.name.trim(), kind, config: mlMonitorConfig(kind, f), auto_investigate: false }));
    if (r) onSaved();
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Watch a model" noValidate>
      <div className="form-row">
        <Field label="Watch for" htmlFor={`${id}-k`} hint={ML_MONITOR_KINDS.find((k) => k.id === kind)?.description}>
          <select id={`${id}-k`} value={kind} onChange={(e) => setKind(e.target.value)}>
            {ML_MONITOR_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
        </Field>
        <Field label="Model" htmlFor={`${id}-m`}>
          <select id={`${id}-m`} value={f.model} onChange={(e) => set("model", e.target.value)}>
            {models.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </Field>
        <Field label="Monitor name" htmlFor={`${id}-n`}><input id={`${id}-n`} value={f.name} onChange={(e) => set("name", e.target.value)} /></Field>
      </div>
      {kind === "ml_drift" && (
        <Field label="PSI threshold" htmlFor={`${id}-psi`}><input id={`${id}-psi`} inputMode="decimal" value={f.psi} onChange={(e) => set("psi", e.target.value)} /></Field>
      )}
      {kind === "ml_freshness" && (
        <Field label="Maximum age (hours)" htmlFor={`${id}-age`}><input id={`${id}-age`} inputMode="numeric" value={f.maxAge} onChange={(e) => set("maxAge", e.target.value)} /></Field>
      )}
      {kind === "ml_performance" && (
        <>
          <div className="form-row">
            <Field label="Label table" htmlFor={`${id}-la`}><input id={`${id}-la`} value={f.labelAsset} onChange={(e) => set("labelAsset", e.target.value)} placeholder="stg_sn.incident" /></Field>
            <Field label="Label column" htmlFor={`${id}-lc`}><input id={`${id}-lc`} value={f.labelColumn} onChange={(e) => set("labelColumn", e.target.value)} placeholder="breached_sla" /></Field>
          </div>
          <div className="form-row">
            <Field label="Label horizon (days)" htmlFor={`${id}-lh`}><input id={`${id}-lh`} inputMode="numeric" value={f.horizon} onChange={(e) => set("horizon", e.target.value)} /></Field>
            <Field label="Tolerance" htmlFor={`${id}-tol`} hint="Allowed loss against the sealed holdout value."><input id={`${id}-tol`} inputMode="decimal" value={f.tolerance} onChange={(e) => set("tolerance", e.target.value)} /></Field>
            <Field label="Minimum matured labels" htmlFor={`${id}-ml`}><input id={`${id}-ml`} inputMode="numeric" value={f.minLabels} onChange={(e) => set("minLabels", e.target.value)} /></Field>
          </div>
        </>
      )}
      {tried && problems.length > 0 && <ul className="small warn-text" role="alert">{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
      <ErrorBox error={act.error} />
      <div className="form-actions"><button type="submit" className="btn btn-primary btn-sm" disabled={act.busy}>Create model monitor</button></div>
    </form>
  );
}

/** Operate → Models & pipelines: model drift, performance and freshness monitors (P5-03), and pipeline health (P6-03). */
export function OperateHealth({ wsId, role }: { wsId: string; role: string | undefined }) {
  const monitors = useAsync(() => api.listMonitors(wsId), [wsId]);
  const models = useAsync(() => api.modelVersions(wsId), [wsId]);
  const [saved, setSaved] = useState(false);
  const ml = (monitors.data ?? []).filter((m: Monitor) => m.kind.startsWith("ml_"));
  const names = [...new Set((models.data ?? []).map((m) => m.name))];
  return (
    <div className="stack">
      <section className="card" aria-label="Model monitors">
        <div className="card-body stack">
          <h2 className="h-sm">Models</h2>
          <ErrorBox error={monitors.error ?? models.error} onRetry={() => { void monitors.reload(); void models.reload(); }} />
          {(monitors.loading && !monitors.data) && <Loading />}
          {monitors.data && ml.length === 0 && <EmptyState title="No model monitors yet">Watch a model for input drift, performance on matured labels and scoring freshness.</EmptyState>}
          {ml.length > 0 && (
            <ul className="list" aria-label="Model monitors">
              {ml.map((m) => (
                <li key={m.id} className="assertion">
                  <div className="chip-row"><strong>{m.name}</strong> <Tag tone="info">{ML_MONITOR_KINDS.find((k) => k.id === m.kind)?.label ?? m.kind}</Tag>
                    <StatusBadge status={m.state} /></div>
                  <span className="small muted">{describeMlMonitor(m.kind, m.config as Record<string, unknown>)}</span>
                  {monitorMessage(m) && <span className={`small ${m.state === "alerting" ? "warn-text" : ""}`}>{monitorMessage(m)}</span>}
                  <span className="small muted">Last evaluated {fmtDate(m.last_evaluated_at)} · <Link to={to.monitoring(wsId, { tab: "alerts" })}>alerts</Link></span>
                </li>
              ))}
            </ul>
          )}
          {saved && <Notice tone="success">Model monitor created. It is evaluated on the monitor schedule; alerts appear under Alerts.</Notice>}
          {roleAtLeast(role, "analyst") && names.length > 0 && (
            <details className="card">
              <summary>Watch a model</summary>
              <div className="card-body"><WatchModelForm wsId={wsId} models={names} onSaved={() => { setSaved(true); void monitors.reload(); }} /></div>
            </details>
          )}
        </div>
      </section>
    </div>
  );
}
