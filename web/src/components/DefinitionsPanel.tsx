import { lazy, Suspense, useId, useState } from "react";
import { api, type DefinitionVersion, type RelationshipCandidate } from "../api";
import { fmtDate, fmtPct, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast } from "../routes";
import { isStaleEdit, StaleEditNotice } from "./StaleEdit";
import { Card, EmptyState, ErrorBox, Loading, Notice, StatusBadge, TechnicalDetails } from "./ui";

const SemanticGraph = lazy(() => import("./SemanticGraph").then((m) => ({ default: m.SemanticGraph })));

const CARDINALITY: Record<string, string> = {
  one_to_one: "one to one", many_to_one: "many to one", one_to_many: "one to many", many_to_many: "many to many",
};

const cols = (asset: string, columns: string[]) => `${asset}.${columns.length > 1 ? `(${columns.join(", ")})` : columns[0] ?? "?"}`;

/**
 * Data → Definitions (spec v4 §15): the relationship review queue with measured cardinality (P7-09),
 * the semantic model diff before approval (P4-05), versioned definitions with draft → publish →
 * deprecate → retire (P7-03), and the semantic graph. Decisions need the approver role and someone
 * other than the proposer; the server enforces both.
 */
export function DefinitionsPanel({ wsId, role }: { wsId: string; role: string | undefined }) {
  return (
    <div className="stack">
      <RelationshipQueue wsId={wsId} role={role} />
      <ModelChanges wsId={wsId} role={role} />
      <Definitions wsId={wsId} role={role} />
      <Card title="Semantic graph">
        <Suspense fallback={<Loading />}><SemanticGraph wsId={wsId} /></Suspense>
      </Card>
    </div>
  );
}

function RelationshipQueue({ wsId, role }: { wsId: string; role: string | undefined }) {
  const list = useAsync(() => api.relationshipCandidates(wsId, "pending"), [wsId]);
  const rows = Array.isArray(list.data) ? list.data : [];
  return (
    <Card title={`Joins to confirm${rows.length ? ` (${rows.length})` : ""}`}>
      <p className="muted small">Each join was measured on the data: how many rows match and how many rows sit on each side.
        Confirming one lets investigations and metrics use it.</p>
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data && rows.length === 0 && <EmptyState title="No joins waiting for review" />}
      <ul className="list" aria-label="Joins to confirm">
        {rows.map((c) => <li key={c.id}><Candidate wsId={wsId} c={c} role={role} onDecided={list.reload} /></li>)}
      </ul>
    </Card>
  );
}

function Candidate({ wsId, c, role, onDecided }: { wsId: string; c: RelationshipCandidate; role: string | undefined; onDecided: () => void }) {
  const id = useId();
  const [reason, setReason] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const act = useAction();
  const canDecide = role === "approver" || role === "owner";
  const canMeasure = roleAtLeast(role, "editor");
  const assessment = c.assessment as { outcome?: string; approvable?: boolean; warnings?: string[] };
  const decide = async (decision: "accept" | "reject") => {
    await api.decideRelationship(wsId, c.id, decision, reason.trim() || undefined);
    setNote(decision === "accept" ? "Confirmed." : "Rejected.");
    onDecided();
  };
  return (
    <article className="candidate" aria-label={`Join ${c.from_asset} to ${c.to_asset}`}>
      <p><strong>{cols(c.from_asset, c.from_columns)}</strong> → <strong>{cols(c.to_asset, c.to_columns)}</strong></p>
      <p className="small">Measured as <strong>{CARDINALITY[c.cardinality] ?? c.cardinality.replace(/_/g, " ")}</strong>;
        {" "}{fmtPct(c.containment, 0)} of values match. Measured {fmtDate(c.measured_at)}.</p>
      {assessment.approvable === false && <Notice tone="warning">The measurement does not support this join well enough to confirm it yet.</Notice>}
      {(assessment.warnings ?? []).map((w) => <p key={w} className="small warn-text">{w}</p>)}
      {canDecide && !note && (
        <div className="form form-inline">
          <label htmlFor={`${id}-reason`} className="sr-only">Reason for the decision on {c.from_asset} to {c.to_asset}</label>
          <input id={`${id}-reason`} placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <button type="button" className="btn btn-sm btn-success" disabled={act.busy} onClick={() => void act.run(() => decide("accept"))}>Confirm</button>
          <button type="button" className="btn btn-sm btn-danger" disabled={act.busy} onClick={() => void act.run(() => decide("reject"))}>Reject</button>
        </div>
      )}
      {canMeasure && !note && (
        <button type="button" className="btn btn-xs btn-ghost" disabled={act.busy}
          onClick={() => void act.run(async () => { await api.measureRelationship(wsId, c.id); onDecided(); })}>Measure again</button>
      )}
      {!canDecide && <p className="muted small">An approver confirms joins; the person who proposed one cannot confirm it.</p>}
      {note && <p className="small" role="status">{note}</p>}
      <ErrorBox error={act.error} />
      <TechnicalDetails value={{ id: c.id, origin: c.origin, content_hash: c.content_hash, evidence: c.evidence, assessment: c.assessment }} />
    </article>
  );
}

const fmtVal = (v: unknown) => (v === undefined || v === null ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v));

function ModelChanges({ wsId, role }: { wsId: string; role: string | undefined }) {
  const diff = useAsync(() => api.semanticModelDiff(wsId), [wsId]);
  const act = useAction();
  const [done, setDone] = useState<string | null>(null);
  const noModel = diff.error && /no semantic model/i.test(diff.error);
  const d = diff.data;
  const canDecide = role === "approver" || role === "owner";
  const decide = async (decision: "approve" | "reject") => {
    if (!d) return;
    if (await act.run(() => api.decideSemanticModel(wsId, d.version, decision))) {
      setDone(decision === "approve" ? `Version ${d.version} approved.` : `Version ${d.version} rejected.`);
      void diff.reload();
    }
  };
  return (
    <Card title="Changes to the data model">
      <p className="muted small">This workspace model maps catalog datasets and measured joins. AI can propose a version, but the compiler uses approved structure; metric expressions are checked against that structure before approval. Review the diff and evidence below before deciding.</p>
      {noModel ? <EmptyState title="No data model yet">It is built from the catalog and confirmed joins.</EmptyState> : <ErrorBox error={diff.error} onRetry={diff.reload} />}
      {!d && !diff.error && <Loading />}
      {d && (
        <>
          <p className="small">Version {d.version} <StatusBadge status={d.status} />
            {d.base_version !== null ? <> compared with the approved version {d.base_version}</> : <> (the first version: everything is new)</>}.</p>
          {!d.has_changes ? <p className="muted small">No changes.</p> : (
            <div className="table-wrap">
              <table className="table table-compact" aria-label="Data model changes">
                <thead><tr><th>Field</th><th>Change</th><th>Before</th><th>After</th></tr></thead>
                <tbody>
                  {d.entries.slice(0, 100).map((e) => (
                    <tr key={e.field}><td><code>{e.field}</code></td><td>{e.change}</td><td className="small">{fmtVal(e.before)}</td><td className="small">{fmtVal(e.after)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {d.status === "proposed" && canDecide && !done && (
            <div className="form-actions">
              <button type="button" className="btn btn-sm btn-danger" disabled={act.busy} onClick={() => void decide("reject")}>Reject version {d.version}</button>
              <button type="button" className="btn btn-sm btn-success" disabled={act.busy} onClick={() => void decide("approve")}>Approve version {d.version}</button>
            </div>
          )}
          {done && <Notice tone="success">{done}</Notice>}
          <ErrorBox error={act.error} />
        </>
      )}
    </Card>
  );
}

const STATUS_WORDS: Record<string, string> = { draft: "draft", published: "published", deprecated: "deprecated (still runs)", retired: "retired" };

function Definitions({ wsId, role }: { wsId: string; role: string | undefined }) {
  const page = useAsync(() => api.listDefinitions(wsId, { include_builtin: true }), [wsId]);
  const [open, setOpen] = useState<string | null>(null);
  const items = page.data?.items ?? [];
  const builtin = page.data?.builtin ?? [];
  return (
    <Card title="Versioned definitions">
      <p className="muted small">Saved analyses, recipes and playbooks. A draft can be edited; publishing freezes a version that schedules pin.
        Deprecated versions keep running with a warning; retired ones never run again.</p>
      <ErrorBox error={page.error} onRetry={page.reload} />
      {page.loading && !page.data && <Loading />}
      {page.data && items.length === 0 && <EmptyState title="No workspace definitions yet" />}
      {items.length > 0 && (
        <ul className="list" aria-label="Definitions">
          {items.map((d) => (
            <li key={d.id}>
              <DefinitionRow wsId={wsId} d={d} role={role} open={open === d.id} onToggle={() => setOpen(open === d.id ? null : d.id)} onChanged={page.reload} />
            </li>
          ))}
        </ul>
      )}
      {builtin.length > 0 && (
        <details>
          <summary>Built-in playbooks ({builtin.length})</summary>
          <ul className="list compact">
            {builtin.map((b) => <li key={b.key} className="list-item"><span>{b.key} <span className="muted small">v{String(b.version)}</span></span>
              <StatusBadge status={b.status ?? "published"} /></li>)}
          </ul>
        </details>
      )}
    </Card>
  );
}

function DefinitionRow({ wsId, d, role, open, onToggle, onChanged }: {
  wsId: string; d: DefinitionVersion; role: string | undefined; open: boolean; onToggle: () => void; onChanged: () => void;
}) {
  const act = useAction();
  const canEdit = roleAtLeast(role, "editor");
  const run = async (fn: () => Promise<unknown>) => {
    if (await act.run(fn)) onChanged();
  };
  return (
    <article className="definition" aria-label={`Definition ${d.title || d.key} version ${d.version}`}>
      <div className="list-item">
        <span><strong>{d.title || d.key}</strong> <span className="muted small">{d.kind.replace(/_/g, " ")} · version {d.version}</span></span>
        <span className="chip-row">
          <StatusBadge status={d.status === "retired" ? "cancelled" : d.status} label={STATUS_WORDS[d.status] ?? d.status} />
          <button type="button" className="btn btn-xs btn-ghost" aria-expanded={open} onClick={onToggle}>Changes</button>
          {canEdit && d.status === "draft" && (
            <button type="button" className="btn btn-xs btn-primary" disabled={act.busy} onClick={() => void run(() => api.publishDefinition(wsId, d.id, d.revision))}>Publish</button>)}
          {canEdit && d.status === "published" && (
            <button type="button" className="btn btn-xs" disabled={act.busy}
              onClick={() => void run(() => api.changeDefinitionStatus(wsId, d.id, "deprecate", d.revision))}>Deprecate</button>)}
          {canEdit && (d.status === "published" || d.status === "deprecated") && (
            <button type="button" className="btn btn-xs btn-danger" disabled={act.busy}
              onClick={() => { if (window.confirm(`Retire ${d.key} version ${d.version}? Schedules pinned to it will stop.`)) void run(() => api.changeDefinitionStatus(wsId, d.id, "retire", d.revision)); }}>
              Retire</button>)}
        </span>
      </div>
      {d.reason && <p className="small muted">{d.reason}</p>}
      {isStaleEdit(act.failure)
        ? <StaleEditNotice what={`${d.key} version ${d.version}`} mine={{ status: d.status, revision: d.revision, content_hash: d.content_hash }}
          loadCurrent={async () => {
            const cur = (await api.listDefinitions(wsId, { kind: d.kind })).items.find((x) => x.id === d.id);
            return cur ? { status: cur.status, revision: cur.revision, content_hash: cur.content_hash } : null;
          }}
          onReload={() => { act.clear(); onChanged(); }} />
        : <ErrorBox error={act.error} />}
      {open && <DefinitionDiff wsId={wsId} id={d.id} />}
      <TechnicalDetails><p className="small">Key <code>{d.key}</code> · content hash <code>{shortHash(d.content_hash, 12)}</code> · revision {d.revision}</p></TechnicalDetails>
    </article>
  );
}

function DefinitionDiff({ wsId, id }: { wsId: string; id: string }) {
  const diff = useAsync(() => api.definitionDiff(wsId, id), [wsId, id]);
  if (diff.error) return <ErrorBox error={diff.error} onRetry={diff.reload} />;
  if (!diff.data) return <Loading />;
  if (!diff.data.to) return <p className="muted small">No published version to compare with.</p>;
  if (!diff.data.changes.length) return <p className="muted small">Identical to version {diff.data.to.version}.</p>;
  return (
    <div className="table-wrap">
      <table className="table table-compact" aria-label={`Changes against version ${diff.data.to.version}`}>
        <thead><tr><th>Field</th><th>This version</th><th>Version {diff.data.to.version}</th></tr></thead>
        <tbody>
          {diff.data.changes.map((c) => <tr key={c.path}><td><code>{c.path}</code></td><td className="small">{fmtVal(c.from)}</td><td className="small">{fmtVal(c.to)}</td></tr>)}
        </tbody>
      </table>
    </div>
  );
}
