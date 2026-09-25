import { useEffect, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, type WorkspaceDetail } from "../api";
import { AutonomyPicker } from "../components/AutonomyPicker";
import { Card, ErrorBox, Field, KeyValue, Loading, Notice, PageHeader, Value } from "../components/ui";
import { useAction, useAsync } from "../lib/hooks";
import { AuditTable } from "./Admin";

const ROLES = ["owner", "editor", "analyst", "approver", "viewer"];

/** Parse policy JSON; returns an error string instead of throwing. */
export function parsePolicy(text: string): { value?: Record<string, unknown>; error?: string } {
  try {
    const v = JSON.parse(text) as unknown;
    if (!v || typeof v !== "object" || Array.isArray(v)) return { error: "policy must be a JSON object" };
    return { value: v as Record<string, unknown> };
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

export function GovernancePage() {
  const { wsId = "" } = useParams();
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  if (ws.error) return <div className="page"><ErrorBox error={ws.error} onRetry={ws.reload} /></div>;
  if (!ws.data) return <div className="page"><Loading /></div>;
  return (
    <div className="page">
      <PageHeader title="Policy & members" subtitle={<>Your role: <strong>{ws.data.role}</strong>. Policy values may only tighten the platform ceilings; each save creates a new immutable version.</>} />
      <PolicySummary ws={ws.data} />
      <div className="grid-2">
        <PolicyEditor ws={ws.data} onSaved={ws.reload} />
        <div className="stack">
          <WorkspaceSettings ws={ws.data} onSaved={ws.reload} />
          <Members ws={ws.data} onChanged={ws.reload} />
        </div>
      </div>
      {ws.data.role === "owner" && <WorkspaceAudit wsId={wsId} />}
    </div>
  );
}

const list = (v: unknown) => (Array.isArray(v) && v.length ? v.map(String).join(", ") : "none");

/** The policy in words: what agents may read, spend and publish here. Unset values show as unknown. */
function PolicySummary({ ws }: { ws: WorkspaceDetail }) {
  const p = ws.policy ?? {};
  return (
    <Card title={`Effective policy · v${ws.policy_version}`}>
      <KeyValue items={[
        ["Max rows per query", <Value key="r" value={p.max_rows} format="int" />],
        ["Run token budget", <Value key="t" value={p.run_token_budget} format="int" />],
        ["Run cost budget", <Value key="c" value={p.run_cost_budget_usd} format="usd" />],
        ["Monthly cost budget", <Value key="m" value={p.workspace_monthly_cost_budget_usd} format="usd" />],
        ["PII access", p.pii_access ?? <Value key="p" value={null} />],
        ["Restricted columns", list(p.restricted_columns)],
        ["Publish destinations", list(p.publish_destinations)],
        ["Publishing needs approval", p.publish_requires_approval === undefined ? <Value key="a" value={null} /> : p.publish_requires_approval ? "yes" : "no"],
        ["Separation of duties", p.separation_of_duties === undefined ? <Value key="s" value={null} /> : p.separation_of_duties ? "yes" : "no"],
        ["Denied tools", list(p.tool_denylist)],
        ["Allowed models", list(p.allowed_models)],
        ["Significance level (α)", <Value key="al" value={p.alpha} format="number" digits={3} />],
      ]} />
    </Card>
  );
}

function PolicyEditor({ ws, onSaved }: { ws: WorkspaceDetail; onSaved: () => void }) {
  const [text, setText] = useState(() => JSON.stringify(ws.policy, null, 2));
  const [saved, setSaved] = useState<number | null>(null);
  const act = useAction();
  useEffect(() => setText(JSON.stringify(ws.policy, null, 2)), [ws.policy]);
  const parsed = parsePolicy(text);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!parsed.value) return;
    const r = await act.run(() => api.putPolicy(ws.id, parsed.value!));
    if (r) {
      setSaved(r.policy_version);
      onSaved();
    }
  };
  return (
    <Card title={`Workspace policy (v${ws.policy_version})`}>
      <form className="form" onSubmit={save}>
        <Field label="Policy document (JSON)" htmlFor="policy-json"
          hint="e.g. max_rows, restricted_columns (schema.table.column or *.column), pii_access, allowed_models, publish_destinations, run_cost_budget_usd.">
          <textarea id="policy-json" className="mono" rows={24} spellCheck={false} value={text} onChange={(e) => { setText(e.target.value); setSaved(null); }}
            aria-invalid={!!parsed.error} />
        </Field>
        {parsed.error && <p className="warn-text small" role="alert">Invalid JSON: {parsed.error}</p>}
        <ErrorBox error={act.error} />
        {saved !== null && <Notice tone="success">Saved as policy version {saved}.</Notice>}
        <div className="form-actions">
          <button type="button" className="btn btn-ghost" onClick={() => setText(JSON.stringify(ws.policy, null, 2))}>Reset</button>
          <button type="submit" className="btn btn-primary" disabled={act.busy || !!parsed.error}>{act.busy ? "Saving…" : "Save policy"}</button>
        </div>
      </form>
    </Card>
  );
}

function WorkspaceSettings({ ws, onSaved }: { ws: WorkspaceDetail; onSaved: () => void }) {
  const [objective, setObjective] = useState(ws.objective);
  const [level, setLevel] = useState(ws.autonomy_level);
  const act = useAction();
  const [ok, setOk] = useState(false);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.updateWorkspace(ws.id, { objective, ...(level !== ws.autonomy_level ? { autonomy_level: level } : {}) }));
    if (r) {
      setOk(true);
      onSaved();
    }
  };
  return (
    <Card title="Objective & autonomy">
      <form className="form" onSubmit={save}>
        <Field label="Objective" htmlFor="gov-obj"><textarea id="gov-obj" rows={3} value={objective} onChange={(e) => { setObjective(e.target.value); setOk(false); }} /></Field>
        <AutonomyPicker value={level} onChange={(v) => { setLevel(v); setOk(false); }} />
        <ErrorBox error={act.error} />
        {ok && <Notice tone="success">Saved.</Notice>}
        <div className="form-actions"><button type="submit" className="btn btn-primary" disabled={act.busy}>Save</button></div>
      </form>
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

function WorkspaceAudit({ wsId }: { wsId: string }) {
  const a = useAsync(() => api.workspaceAudit(wsId, 200), [wsId]);
  const [q, setQ] = useState("");
  if (a.error) return <ErrorBox error={a.error} />;
  if (!a.data) return <Loading />;
  const needle = q.toLowerCase();
  return <AuditTable rows={a.data.filter((e) => !needle || `${e.actor} ${e.action} ${e.target ?? ""}`.toLowerCase().includes(needle))} filter={q} onFilter={setQ} />;
}
