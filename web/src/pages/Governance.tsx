import { useEffect, useId, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, type WorkspaceDetail } from "../api";
import { useAuth } from "../auth";
import { AutonomyPicker } from "../components/AutonomyPicker";
import { Card, ErrorBox, Field, Loading, Notice, PageHeader, StateView } from "../components/ui";
import { useAction, useAsync } from "../lib/hooks";
import { fieldText, fieldValue, parsePolicy, POLICY_FIELDS, setField } from "../lib/policy";
import { autonomyInWords } from "../lib/status";
import { AuditTable } from "./Admin";

export { parsePolicy };

const ROLES = ["owner", "editor", "analyst", "approver", "viewer"];

/**
 * Settings → Members & policy (spec v4 §15): a form for the common policy fields, with the raw JSON
 * and the autonomy level under Advanced (their one home), members, and the one audit view.
 */
export function GovernancePage() {
  const { wsId = "" } = useParams();
  const { user } = useAuth();
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  if (ws.error) return <div className="page"><ErrorBox error={ws.error} onRetry={ws.reload} /></div>;
  if (!ws.data) return <div className="page"><Loading /></div>;
  if (ws.data.role !== "owner" && !user?.is_admin) {
    return (
      <div className="page">
        <PageHeader title="Members & policy" />
        <StateView kind="not-entitled" title="Workspace owners manage members and policy">
          Your role here is {ws.data.role}. The limits that apply to you are shown before any work starts.
        </StateView>
      </div>
    );
  }
  return (
    <div className="page">
      <PageHeader title="Members & policy" subtitle={<>Policy values may only tighten the platform limits; each save creates a new version.</>} />
      <div className="grid-2">
        <PolicyEditor ws={ws.data} onSaved={ws.reload} />
        <div className="stack">
          <Members ws={ws.data} onChanged={ws.reload} />
          <Autonomy ws={ws.data} onSaved={ws.reload} />
        </div>
      </div>
      <Audit wsId={wsId} isAdmin={!!user?.is_admin} />
    </div>
  );
}

/** The common fields as a form; the whole document as JSON under Advanced. Both edit one draft. */
function PolicyEditor({ ws, onSaved }: { ws: WorkspaceDetail; onSaved: () => void }) {
  const id = useId();
  const [draft, setDraft] = useState<Record<string, unknown>>(() => ({ ...ws.policy }));
  const [text, setText] = useState(() => JSON.stringify(ws.policy, null, 2));
  const [saved, setSaved] = useState<number | null>(null);
  const act = useAction();
  useEffect(() => {
    setDraft({ ...ws.policy });
    setText(JSON.stringify(ws.policy, null, 2));
  }, [ws.policy]);
  const parsed = parsePolicy(text);
  const edit = (next: Record<string, unknown>) => {
    setDraft(next);
    setText(JSON.stringify(next, null, 2));
    setSaved(null);
  };
  const editJson = (value: string) => {
    setText(value);
    setSaved(null);
    const p = parsePolicy(value);
    if (p.value) setDraft(p.value);
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!parsed.value) return;
    const r = await act.run(() => api.putPolicy(ws.id, parsed.value!));
    if (r) {
      setSaved(r.policy_version);
      onSaved();
    }
  };
  const reset = () => edit({ ...ws.policy });
  return (
    <Card title={`Effective policy · version ${ws.policy_version}`}>
      <form className="form" onSubmit={save} aria-label="Workspace policy">
        {POLICY_FIELDS.map((f) => {
          const fid = `${id}-${f.key}`;
          const v = fieldText(f.kind, draft[f.key]);
          if (f.kind === "bool") {
            return (
              <label key={f.key} className="check-row">
                <input type="checkbox" checked={v === true} onChange={(e) => edit(setField(draft, f.key, fieldValue("bool", e.target.checked)))} />
                <span>{f.label}</span><span className="field-hint">{f.hint}</span>
              </label>
            );
          }
          return (
            <Field key={f.key} label={f.label} htmlFor={fid} hint={f.hint}>
              {f.kind === "pii" ? (
                <select id={fid} value={String(v)} onChange={(e) => edit(setField(draft, f.key, fieldValue("pii", e.target.value)))}>
                  <option value="">Platform default</option>
                  <option value="none">Never</option>
                  <option value="restricted">Masked</option>
                  <option value="allowed">Allowed</option>
                </select>
              ) : f.kind === "list" ? (
                <textarea id={fid} rows={2} value={String(v)} onChange={(e) => edit(setField(draft, f.key, fieldValue("list", e.target.value)))} />
              ) : (
                <input id={fid} type="number" min={0} step={f.kind === "usd" ? "0.01" : "1"} value={String(v)} placeholder="Platform default"
                  onChange={(e) => edit(setField(draft, f.key, fieldValue(f.kind, e.target.value)))} />
              )}
            </Field>
          );
        })}
        <details className="advanced">
          <summary>Advanced: the whole policy as JSON</summary>
          <Field label="Policy document (JSON)" htmlFor={`${id}-json`}
            hint="Every field, including ones the form does not show (allowed models, denied tools, significance level, …).">
            <textarea id={`${id}-json`} className="mono" rows={16} spellCheck={false} value={text} onChange={(e) => editJson(e.target.value)}
              aria-invalid={!!parsed.error} />
          </Field>
          {parsed.error && <p className="warn-text small" role="alert">Invalid JSON: {parsed.error}</p>}
        </details>
        <ErrorBox error={act.error} />
        {saved !== null && <Notice tone="success">Saved as policy version {saved}.</Notice>}
        <div className="form-actions">
          <button type="button" className="btn btn-ghost" onClick={reset}>Reset</button>
          <button type="submit" className="btn btn-primary" disabled={act.busy || !!parsed.error}>{act.busy ? "Saving…" : "Save policy"}</button>
        </div>
      </form>
    </Card>
  );
}

/** Autonomy's one home: in plain words, the level code only inside Advanced. */
function Autonomy({ ws, onSaved }: { ws: WorkspaceDetail; onSaved: () => void }) {
  const [level, setLevel] = useState(ws.autonomy_level);
  const act = useAction();
  const [ok, setOk] = useState(false);
  useEffect(() => setLevel(ws.autonomy_level), [ws.autonomy_level]);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (await act.run(() => api.updateWorkspace(ws.id, { autonomy_level: level }))) {
      setOk(true);
      onSaved();
    }
  };
  return (
    <Card title="How much agents do alone">
      <p className="small">{autonomyInWords(ws.autonomy_level)}</p>
      <details className="advanced">
        <summary>Advanced: change the autonomy level</summary>
        <form className="form" onSubmit={save}>
          <AutonomyPicker value={level} onChange={(v) => { setLevel(v); setOk(false); }} />
          <ErrorBox error={act.error} />
          {ok && <Notice tone="success">Saved.</Notice>}
          <div className="form-actions"><button type="submit" className="btn btn-primary" disabled={act.busy || level === ws.autonomy_level}>Save</button></div>
        </form>
      </details>
    </Card>
  );
}

function Members({ ws, onChanged }: { ws: WorkspaceDetail; onChanged: () => void }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("analyst");
  const act = useAction();
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.addMember(ws.id, email.trim(), role));
    if (r) {
      setEmail("");
      onChanged();
    }
  };
  const remove = async (userId: string, label: string) => {
    if (!window.confirm(`Remove ${label} from this workspace?`)) return;
    const r = await act.run(() => api.removeMember(ws.id, userId));
    if (r) onChanged();
  };
  return (
    <Card title={`Members (${ws.members.length})`}>
      <ul className="list compact">
        {ws.members.map((m) => (
          <li key={m.user_id} className="list-item">
            <span>{m.name || m.email} <span className="muted small">{m.email}</span></span>
            <span className="chip-row"><span className="tag">{m.role}</span>
              <button type="button" className="btn btn-xs btn-ghost" onClick={() => remove(m.user_id, m.email)} disabled={act.busy} aria-label={`Remove ${m.email}`}>Remove</button>
            </span>
          </li>
        ))}
      </ul>
      <form className="form form-inline" onSubmit={add}>
        <label className="sr-only" htmlFor="mem-email">Email</label>
        <input id="mem-email" type="email" required placeholder="approver@analystos.local" value={email} onChange={(e) => setEmail(e.target.value)} />
        <label className="sr-only" htmlFor="mem-role">Role</label>
        <select id="mem-role" value={role} onChange={(e) => setRole(e.target.value)}>{ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</select>
        <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy}>Add member</button>
      </form>
      <ErrorBox error={act.error} />
    </Card>
  );
}

/** The one audit view: this workspace, or (platform admins) the whole platform. */
function Audit({ wsId, isAdmin }: { wsId: string; isAdmin: boolean }) {
  const [scope, setScope] = useState<"workspace" | "platform">("workspace");
  const a = useAsync(() => (scope === "platform" ? api.audit(300) : api.workspaceAudit(wsId, 200)), [wsId, scope]);
  const [q, setQ] = useState("");
  const needle = q.toLowerCase();
  return (
    <section className="stack" aria-label="Audit log">
      {isAdmin && (
        <div className="seg" role="radiogroup" aria-label="Audit scope">
          {(["workspace", "platform"] as const).map((s) => (
            <button key={s} type="button" role="radio" aria-checked={scope === s} className={`seg-btn ${scope === s ? "active" : ""}`}
              onClick={() => setScope(s)}>{s === "workspace" ? "This workspace" : "Whole platform"}</button>
          ))}
        </div>
      )}
      <ErrorBox error={a.error} onRetry={a.reload} />
      {!a.data && !a.error && <Loading />}
      {a.data && <AuditTable rows={a.data.filter((e) => !needle || `${e.actor} ${e.action} ${e.target ?? ""} ${e.decision ?? ""}`.toLowerCase().includes(needle))}
        filter={q} onFilter={setQ} />}
    </section>
  );
}
