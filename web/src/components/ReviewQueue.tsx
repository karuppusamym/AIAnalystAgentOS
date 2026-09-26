import { useId, useMemo, useState } from "react";
import { api, type KnowledgeSuggestion, type ReviewResult } from "../api";
import { batchDecisions, editableFields, editedFields, fieldText, primaryField, provenanceLine } from "../lib/knowledge";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { Card, ConfidenceBar, EmptyState, ErrorBox, Loading, Notice, Tag } from "./ui";

const STATUSES = ["pending", "approved", "rejected", "superseded"] as const;
const KIND_LABEL: Record<string, string> = {
  term: "glossary term", definition: "definition", metric: "metric", rule: "business rule", note: "note", negative: "negative knowledge",
  attested_computation: "attested computation", table_description: "table description",
};

/**
 * Knowledge → Review queue (P4-K07 data, P4-U04 screen): AI and learning-loop drafts with each
 * field's value, confidence and provenance. An editor approves or rejects a selection in one batch
 * (one pack revision), or edits a draft and approves it; a rejection needs a reason and becomes
 * negative knowledge that later prompts see.
 */
export function ReviewQueue({ wsId, canDecide, onOpenDocument }: {
  wsId: string; canDecide: boolean; onOpenDocument: (path: string) => void;
}) {
  const id = useId();
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("pending");
  const list = useAsync(() => api.knowledgeSuggestions(wsId, status), [wsId, status]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const [reasonMissing, setReasonMissing] = useState(false);
  const [result, setResult] = useState<ReviewResult | null>(null);
  const act = useAction();
  const rows = useMemo(() => list.data ?? [], [list.data]);
  const pending = status === "pending";
  const allOn = rows.length > 0 && rows.every((r) => selected.has(r.id));

  const toggle = (sid: string, on: boolean) => setSelected((s) => {
    const n = new Set(s);
    if (on) n.add(sid);
    else n.delete(sid);
    return n;
  });
  const decide = async (decisions: Parameters<typeof api.reviewSuggestions>[1]) => {
    const r = await act.run(() => api.reviewSuggestions(wsId, decisions));
    if (r) {
      setResult(r);
      setSelected(new Set());
      setReason("");
      await list.reload();
    }
  };
  const batch = async (action: "approve" | "reject") => {
    if (action === "reject" && !reason.trim()) {
      setReasonMissing(true);
      return;
    }
    setReasonMissing(false);
    await decide(batchDecisions([...selected], action, reason));
  };

  return (
    <div className="stack">
      <div className="chip-row" role="toolbar" aria-label="Filter the review queue">
        {STATUSES.map((s) => (
          <button key={s} type="button" className={`chip ${status === s ? "active" : ""}`} aria-pressed={status === s}
            onClick={() => { setStatus(s); setSelected(new Set()); setResult(null); }}>{s}</button>
        ))}
      </div>
      {result && (
        <Notice tone={result.errors.length ? "warning" : "success"}>
          {result.revision !== null ? <>Workspace pack revision {result.revision}: </> : <>No revision written: </>}
          {result.approved.length} approved, {result.rejected.length} rejected
          {result.rejected.length > 0 && <> (recorded as negative knowledge)</>}.
          {result.approved.filter((a) => a.path).map((a) => (
            <button key={a.id} type="button" className="btn btn-xs btn-ghost" onClick={() => onOpenDocument(a.path!)}>Open {a.path}</button>
          ))}
          {result.errors.length > 0 && (
            <ul className="small warn-list">{result.errors.map((e) => <li key={e.id}><code>{e.id}</code>: {e.error.replace(/_/g, " ")}</li>)}</ul>
          )}
        </Notice>
      )}
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data && rows.length === 0 && (
        <EmptyState title={pending ? "Nothing to review" : `No ${status} drafts`}>
          {pending ? "Crawls, accepted findings, approved KPIs and run feedback propose knowledge here." : null}
        </EmptyState>
      )}
      {pending && rows.length > 0 && !canDecide && <p className="small" role="note">Editors and owners decide drafts; you can read them.</p>}
      {pending && rows.length > 0 && canDecide && (
        <Card>
          <div className="review-batch">
            <label className="toggle small">
              <input type="checkbox" checked={allOn} onChange={(e) => setSelected(e.target.checked ? new Set(rows.map((r) => r.id)) : new Set())} />
              Select all ({rows.length})
            </label>
            <label className="sr-only" htmlFor={`${id}-reason`}>Reason for rejecting the selection</label>
            <input id={`${id}-reason`} value={reason} placeholder="Reason (required to reject)" maxLength={2000}
              onChange={(e) => { setReason(e.target.value); setReasonMissing(false); }}
              aria-invalid={reasonMissing || undefined} aria-describedby={reasonMissing ? `${id}-reason-err` : undefined} />
            <button type="button" className="btn btn-success btn-sm" disabled={!selected.size || act.busy} onClick={() => void batch("approve")}>
              Approve selected ({selected.size})</button>
            <button type="button" className="btn btn-danger btn-sm" disabled={!selected.size || act.busy} onClick={() => void batch("reject")}>
              Reject selected ({selected.size})</button>
          </div>
          {reasonMissing && <p id={`${id}-reason-err`} className="field-error" role="alert">Say why: a rejection is kept as negative knowledge.</p>}
          <ErrorBox error={act.error} />
        </Card>
      )}
      {rows.length > 0 && (
        <ul className="stack review-list" aria-label="Knowledge drafts">
          {rows.map((s) => (
            <li key={s.id}>
              <SuggestionCard s={s} selectable={pending && canDecide} selected={selected.has(s.id)} onSelect={(on) => toggle(s.id, on)}
                busy={act.busy} onEditApprove={(fields) => void decide([{ id: s.id, action: "edit", fields }])} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** A catalog field's previous value (`{value, origin}`), shown so a reviewer sees what approval replaces. */
function beforeText(before: unknown): string {
  const v = before && typeof before === "object" && "value" in before ? (before as { value: unknown }).value : before;
  return v === null || v === undefined ? "" : String(v);
}

function SuggestionCard({ s, selectable, selected, onSelect, busy, onEditApprove }: {
  s: KnowledgeSuggestion; selectable: boolean; selected: boolean; onSelect: (on: boolean) => void; busy: boolean;
  onEditApprove: (fields: Record<string, unknown>) => void;
}) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const editable = editableFields(s);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const begin = () => {
    setEdits(Object.fromEntries(editable.map((k) => [k, fieldText(s.fields[k])])));
    setEditing(true);
  };
  const main = primaryField(s.kind);
  const names = Object.keys(s.fields).sort((a, b) => (a === main ? -1 : b === main ? 1 : a.localeCompare(b)));
  return (
    <article className={`card suggestion ${selected ? "suggestion-selected" : ""}`} aria-label={`Draft: ${s.title}`}>
      <div className="card-body stack">
        <div className="suggestion-head">
          {selectable && (
            <input type="checkbox" id={`${id}-sel`} checked={selected} onChange={(e) => onSelect(e.target.checked)} aria-label={`Select ${s.title}`} />
          )}
          <div className="suggestion-title">
            <h2 className="h-md">{s.title}</h2>
            <span className="chip-row small">
              <Tag tone="info">{KIND_LABEL[s.kind] ?? s.kind}</Tag>
              <span className="muted">{s.origin} · {s.proposed_by} · {fmtDate(s.created_at)}</span>
              {s.status !== "pending" && <Tag tone={s.status === "approved" ? "success" : s.status === "rejected" ? "danger" : "neutral"}>{s.status}</Tag>}
            </span>
          </div>
          <div className="suggestion-confidence" title="The least confident field"><ConfidenceBar value={s.confidence} /></div>
        </div>
        <p className="small muted">Writes <code>{s.path}</code> · subject <code>{s.subject}</code>{s.revision ? <> · revision {s.revision}</> : null}</p>
        {s.reason && <p className="small">Reason: {s.reason}</p>}
        {!editing && (
          <table className="table table-compact suggestion-fields">
            <caption className="sr-only">Fields of {s.title}</caption>
            <thead><tr><th scope="col">Field</th><th scope="col">Value</th><th scope="col">Confidence</th><th scope="col">Provenance</th></tr></thead>
            <tbody>
              {names.map((name) => {
                const f = s.fields[name];
                return (
                  <tr key={name}>
                    <th scope="row"><code>{name}</code></th>
                    <td className="small"><span className="clamp-3">{fieldText(f)}</span>
                      {f.before !== undefined && <div className="muted small">was: {beforeText(f.before) || "empty"}</div>}</td>
                    <td><ConfidenceBar value={f.confidence} /></td>
                    <td className="small">
                      <ul className="plain-list">{provenanceLine(f.provenance).map(([k, v]) => <li key={k}><span className="muted">{k}:</span> {v}</li>)}</ul>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {editing && (
          <form className="form" aria-label={`Edit ${s.title}`} onSubmit={(e) => { e.preventDefault(); onEditApprove(editedFields(s, edits)); }}>
            {editable.map((name) => (
              <div className="field" key={name}>
                <label htmlFor={`${id}-${name}`}><code>{name}</code>{Array.isArray(s.fields[name].value) ? " (comma-separated)" : ""}</label>
                <textarea id={`${id}-${name}`} rows={name === main ? 4 : 1} value={edits[name] ?? ""}
                  onChange={(e) => setEdits((x) => ({ ...x, [name]: e.target.value }))} />
              </div>
            ))}
            <p className="muted small">Edited fields are recorded as yours (provenance <code>human</code>, confidence 100%); the others keep their provenance.</p>
            <div className="form-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
              <button type="submit" className="btn btn-success" disabled={busy}>Approve with edits</button>
            </div>
          </form>
        )}
        {selectable && !editing && editable.length > 0 && (
          <div className="form-actions">
            <button type="button" className="btn btn-sm" onClick={begin}>Edit, then approve</button>
          </div>
        )}
      </div>
    </article>
  );
}
