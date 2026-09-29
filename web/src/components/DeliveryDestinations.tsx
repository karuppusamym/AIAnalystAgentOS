import { useId, useState, type FormEvent } from "react";
import { api, type Delivery, type DeliveryDestination } from "../api";
import { Card, ErrorBox, Field, Notice, StatusBadge, Tag } from "./ui";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";

/** N-3: where reports and alerts may be sent outside the platform. A destination sends nothing until an
 *  approver authorizes it in the approvals inbox; changing its target asks for a new authorization. */
export function DeliveryDestinations({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const list = useAsync(() => api.listDeliveryDestinations(wsId), [wsId]);
  const deliveries = useAsync(() => api.listDeliveries(wsId), [wsId]);
  const [adding, setAdding] = useState(false);
  const act = useAction();
  const revoke = async (d: DeliveryDestination) => {
    if (!window.confirm(`Revoke “${d.name}”? Queued deliveries to it are refused.`)) return;
    if (await act.run(() => api.revokeDeliveryDestination(d.id))) { void list.reload(); void deliveries.reload(); }
  };
  const reauthorize = async (d: DeliveryDestination) => {
    if (await act.run(() => api.reauthorizeDeliveryDestination(d.id))) void list.reload();
  };
  const redrive = async (d: Delivery) => {
    if (await act.run(() => api.redriveDelivery(d.id))) void deliveries.reload();
  };
  const names = new Map((list.data ?? []).map((d) => [d.id, d.name]));
  return (
    <Card title="External delivery" label="External delivery destinations"
      actions={canEdit && !adding && <button type="button" className="btn btn-sm" onClick={() => setAdding(true)}>Add destination</button>}>
      <p className="muted small">Email recipients and webhooks that scheduled reports and monitor alerts may be sent to. Each needs an
        approver's authorization, bound to the exact target; any change needs a new one.</p>
      {adding && <DestinationForm wsId={wsId} onCancel={() => setAdding(false)} onSaved={() => { setAdding(false); void list.reload(); }} />}
      <ErrorBox error={list.error ?? act.error} onRetry={list.reload} />
      {list.data?.length === 0 && !adding && <p className="muted small">No destinations yet.</p>}
      {!!list.data?.length && (
        <div className="table-wrap">
          <table className="table table-compact" aria-label="Delivery destinations">
            <thead><tr><th>Name</th><th>Target</th><th>Sends</th><th>Status</th><th>ID</th><th /></tr></thead>
            <tbody>
              {list.data.map((d) => (
                <tr key={d.id}>
                  <td>{d.name} <Tag tone="info">{d.kind}</Tag></td>
                  <td className="small">{d.summary}</td>
                  <td className="small">{d.content_kinds.join(", ")}</td>
                  <td>
                    <StatusBadge status={d.authorized ? "authorized" : d.status} />
                    {d.status === "pending" && <span className="muted small"> awaiting approval</span>}
                    {d.status === "lapsed" && <span className="muted small"> authorization lapsed: request it again</span>}
                    {d.authorized && d.authorized_until && <span className="muted small"> until {fmtDate(d.authorized_until)}</span>}
                  </td>
                  <td className="small mono">{d.id}</td>
                  <td>
                    {canEdit && (d.status === "lapsed" || d.status === "rejected") &&
                      <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void reauthorize(d)}>Request again</button>}
                    {canEdit && d.status !== "revoked" &&
                      <button type="button" className="btn btn-sm btn-danger" disabled={act.busy} onClick={() => void revoke(d)}>Revoke</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!!deliveries.data?.length && (
        <details className="recent-runs">
          <summary className="small">Recent deliveries ({deliveries.data.length})</summary>
          <div className="table-wrap">
            <table className="table table-compact" aria-label="Recent deliveries">
              <thead><tr><th>Status</th><th>What</th><th>To</th><th>Attempts</th><th>When</th><th>Error</th><th /></tr></thead>
              <tbody>
                {deliveries.data.map((d) => (
                  <tr key={d.id}>
                    <td><StatusBadge status={d.status} /></td>
                    <td className="small">{d.subject_type} {d.subject_id}</td>
                    <td className="small">{names.get(d.destination_id) ?? d.destination_id}</td>
                    <td className="small">{d.attempts}/{d.max_attempts}</td>
                    <td className="small">{fmtDate(d.delivered_at ?? d.next_attempt_at ?? d.created_at)}</td>
                    <td className="small warn-text clamp-2">{d.last_error ?? ""}</td>
                    <td>{canEdit && (d.status === "dead_letter" || d.status === "refused") &&
                      <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void redrive(d)}>Retry</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </Card>
  );
}

function DestinationForm({ wsId, onSaved, onCancel }: { wsId: string; onSaved: () => void; onCancel: () => void }) {
  const id = useId();
  const [kind, setKind] = useState<"email" | "webhook">("email");
  const [name, setName] = useState("");
  const [recipients, setRecipients] = useState("");
  const [url, setUrl] = useState("");
  const [secretRef, setSecretRef] = useState("env:ANALYSTOS_WEBHOOK_");
  const [kinds, setKinds] = useState<string[]>(["report", "alert"]);
  const [saved, setSaved] = useState<DeliveryDestination | null>(null);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const config = kind === "email"
      ? { recipients: recipients.split(/[,;\s]+/).filter(Boolean) }
      : { url: url.trim(), secret_ref: secretRef.trim() };
    const r = await act.run(() => api.createDeliveryDestination(wsId, { name: name.trim(), kind, config, content_kinds: kinds }));
    if (r) setSaved(r);
  };
  if (saved) {
    return (
      <Notice tone="success">
        “{saved.name}” was registered and an authorization was requested (approval {saved.approval?.id}). It sends nothing until an
        approver decides. <button type="button" className="btn btn-sm btn-ghost" onClick={onSaved}>Done</button>
      </Notice>
    );
  }
  return (
    <form className="form" onSubmit={submit} aria-label="New delivery destination">
      <div className="form-row">
        <Field label="Name" htmlFor={`${id}-name`}>
          <input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} required />
        </Field>
        <Field label="Kind" htmlFor={`${id}-kind`}>
          <select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value as "email" | "webhook")}>
            <option value="email">Email</option>
            <option value="webhook">Webhook</option>
          </select>
        </Field>
      </div>
      {kind === "email" ? (
        <Field label="Recipients" htmlFor={`${id}-to`} hint="Comma separated addresses.">
          <input id={`${id}-to`} value={recipients} onChange={(e) => setRecipients(e.target.value)} />
        </Field>
      ) : (
        <div className="form-row">
          <Field label="Webhook URL" htmlFor={`${id}-url`} hint="https only; internal addresses are refused unless an operator allows them.">
            <input id={`${id}-url`} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://hooks.example.com/analystos" />
          </Field>
          <Field label="Signing secret reference" htmlFor={`${id}-sec`}
            hint="env:ANALYSTOS_WEBHOOK_<NAME>, the variable holding the HMAC secret (16+ characters) — never the secret itself.">
            <input id={`${id}-sec`} className="mono" value={secretRef} onChange={(e) => setSecretRef(e.target.value)} />
          </Field>
        </div>
      )}
      <fieldset className="autonomy">
        <legend>Sends</legend>
        <div className="toggle-group">
          {["report", "alert"].map((k) => (
            <label key={k} className="toggle small">
              <input type="checkbox" checked={kinds.includes(k)}
                onChange={(e) => setKinds(e.target.checked ? [...kinds, k] : kinds.filter((x) => x !== k))} /> {k}s
            </label>
          ))}
        </div>
      </fieldset>
      <ErrorBox error={act.error} />
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={act.busy}>Request authorization</button>
      </div>
    </form>
  );
}
