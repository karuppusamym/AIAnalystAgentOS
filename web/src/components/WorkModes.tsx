import { useEffect, useState } from "react";
import { api, type WorkMode, type WorkModesPlan } from "../api";
import { useAction, useAsync } from "../lib/hooks";
import { Card, ErrorBox, Loading } from "./ui";

export const WORK_MODES: { id: WorkMode; label: string; detail: string }[] = [
  { id: "analysis", label: "Analysis", detail: "Questions, comparisons and verified findings" },
  { id: "engineering", label: "Data engineering", detail: "Recipes, data preparation and builds" },
  { id: "ml", label: "ML and experiments", detail: "Training and scoring from published definitions" },
];

export function WorkModeChoices({ selected, onChange }: { selected: WorkMode[]; onChange: (modes: WorkMode[]) => void }) {
  return <fieldset className="stack">
    <legend>What work will this workspace support?</legend>
    {WORK_MODES.map((mode) => <label key={mode.id} className="block">
      <input type="checkbox" checked={selected.includes(mode.id)} onChange={(e) =>
        onChange(e.target.checked ? [...selected, mode.id] : selected.filter((m) => m !== mode.id))} />
      <strong> {mode.label}</strong><span className="small muted block">{mode.detail}</span>
    </label>)}
  </fieldset>;
}

/** Owners review the exact playbook changes before applying a preset. */
export function WorkModesSettings({ wsId, collapsed = false }: { wsId: string; collapsed?: boolean }) {
  const loaded = useAsync(() => api.getWorkModes(wsId), [wsId]);
  const [selected, setSelected] = useState<WorkMode[]>([]);
  const [plan, setPlan] = useState<WorkModesPlan | null>(null);
  const action = useAction();
  useEffect(() => { if (loaded.data) setSelected(loaded.data.current); }, [loaded.data]);
  const change = (modes: WorkMode[]) => { setSelected(modes); setPlan(null); };
  const review = async () => {
    const result = await action.run(() => api.previewWorkModes(wsId, selected));
    if (result) setPlan(result);
  };
  const save = async () => {
    if (!plan || plan.selected.length !== selected.length || !plan.selected.every((m) => selected.includes(m))) return;
    const result = await action.run(() => api.setWorkModes(wsId, selected));
    if (result) { loaded.setData(result); setPlan(null); }
  };
  const body = <>
    <p className="small muted">Choose one or more. This changes work shortcuts and playbook enablement. Data grants, roles and approvals are managed separately.</p>
    <ErrorBox error={loaded.error ?? action.error} onRetry={loaded.reload} />
    {!loaded.data ? <Loading /> : <>
      <WorkModeChoices selected={selected} onChange={change} />
      {selected.length === 0 && <p className="warn-text small">Select at least one work mode.</p>}
      <div className="form-actions"><button type="button" className="btn" disabled={action.busy || !selected.length}
        onClick={() => void review()}>Review changes</button></div>
      {plan && <div className="stack" role="status">
        <p className="small">{plan.capabilities.filter((c) => c.changed).length} playbook settings will change.</p>
        <ul className="list compact">{plan.capabilities.filter((c) => c.changed || !c.available).map((c) =>
          <li key={c.capability_id}>{c.capability_id}: {c.available ? c.enabled ? "enable" : "disable" : "not installed"}</li>)}</ul>
        <button type="button" className="btn btn-primary" disabled={action.busy} onClick={() => void save()}>Save work modes</button>
      </div>}
    </>}
  </>;
  // Configuration, not something that needs the person: on the Overview it stays closed until asked for.
  if (collapsed) return <details className="card at-a-glance"><summary>Work modes</summary><div className="card-body">{body}</div></details>;
  return <Card title="Workspace work modes">{body}</Card>;
}
