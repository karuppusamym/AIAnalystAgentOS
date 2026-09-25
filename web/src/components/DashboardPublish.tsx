import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Approval, type Artifact } from "../api";
import { fmtDate, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";
import { DashboardPreview } from "./DashboardPreview";
import { Card, EmptyState, ErrorBox, KeyValue, Loading, StatusBadge } from "./ui";

/**
 * Build → Dashboards (P4-U05): the native ECharts preview of a designed dashboard, then publication.
 * Publishing stays proposal-based: the button fetches the run's pending, hash-bound publication
 * proposal and sends the person to the approvals inbox to decide it; nothing is written from here.
 */
export function DashboardsPanel({ wsId, selected, onSelect }: { wsId: string; selected: string | null; onSelect: (id: string | null) => void }) {
  const list = useAsync(() => api.listArtifacts(wsId, { type: "dashboard" }), [wsId]);
  return (
    <div className="stack">
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data?.length === 0 && <EmptyState title="No dashboards yet">Investigations design dashboards from their verified findings.</EmptyState>}
      {!!list.data?.length && (
        <div className="split">
          <div className="split-list">
            <ul className="list selectable" aria-label="Dashboards">
              {list.data.map((a) => (
                <li key={a.id}>
                  <button type="button" className={`list-button ${a.id === selected ? "active" : ""}`} onClick={() => onSelect(a.id)}
                    aria-current={a.id === selected ? "true" : undefined}>
                    <span className="list-button-head"><span className="clamp-1">{a.name}</span><StatusBadge status={a.status} /></span>
                    <span className="chip-row small"><span className="muted">v{a.version}</span><span className="muted">{fmtDate(a.created_at)}</span></span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div className="split-detail">
            {selected ? <DashboardDetail id={selected} wsId={wsId} />
              : <EmptyState title="Select a dashboard">It renders natively from its charts' data before anything is published.</EmptyState>}
          </div>
        </div>
      )}
    </div>
  );
}

function DashboardDetail({ id, wsId }: { id: string; wsId: string }) {
  const d = useAsync(() => api.getArtifact(id), [id]);
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />;
  if (!d.data) return <Loading />;
  return (
    <div className="stack">
      <PublishCard wsId={wsId} artifact={d.data} />
      <DashboardPreview wsId={wsId} artifact={d.data} />
    </div>
  );
}

function PublishCard({ wsId, artifact }: { wsId: string; artifact: Artifact }) {
  const act = useAction();
  const [proposal, setProposal] = useState<Approval | null>(null);
  const published = artifact.status === "published";
  const request = async () => {
    const r = await act.run(() => api.publishDashboard(artifact.id));
    if (r) setProposal(r);
  };
  return (
    <Card title="Publish" actions={<StatusBadge status={artifact.status} />}>
      {published ? (
        <p className="small">Published to {artifact.platform ?? "BI"}{artifact.external_url && <> — <a href={artifact.external_url} target="_blank" rel="noreferrer noopener">open ↗</a></>}.</p>
      ) : (
        <>
          <p className="small muted">
            Publication writes outside the platform, so it is a proposal bound to the payload hash, plan hash and policy version, decided by an
            approver in the inbox. The preview below is what the proposal publishes.
          </p>
          {!proposal && (
            <button type="button" className="btn btn-primary btn-sm" disabled={act.busy} onClick={() => void request()}>
              {act.busy ? "Checking…" : "Request publication"}</button>
          )}
        </>
      )}
      {proposal && (
        <div className="stack">
          <KeyValue items={[
            ["Proposal", <code key="i">{proposal.id}</code>],
            ["Status", <StatusBadge key="s" status={proposal.status} />],
            ["Destination", proposal.destination ?? "—"],
            ["Payload hash", <code key="h" title={proposal.payload_hash}>{shortHash(proposal.payload_hash, 16)}</code>],
            ["Expires", fmtDate(proposal.expires_at)],
          ]} />
          <p><Link className="btn btn-sm" to={to.approvals(wsId)}>Decide in the approvals inbox</Link></p>
        </div>
      )}
      <ErrorBox error={act.error} />
    </Card>
  );
}
