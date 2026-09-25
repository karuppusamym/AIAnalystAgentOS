import { useState } from "react";
import { api, type Approval } from "../api";
import { fmtDate, fmtPct, shortHash } from "../lib/format";
import { useAction } from "../lib/hooks";
import { EmptyState, ErrorBox, JsonView, KeyValue, StatusBadge, Tag } from "./ui";

const ACTION_LABEL: Record<string, string> = {
  publish_dashboard: "Publish dashboards",
  execute_plan: "Execute analysis plan",
};

function GovernanceReview({ evidence }: { evidence: Record<string, unknown> }) {
  const review = evidence.governance_review as { ok?: boolean; problems?: string[] } | undefined;
  const policy = evidence.policy as { decision?: string; reasons?: string[]; risk_tier?: string; effective_autonomy?: number } | undefined;
  const jev = evidence.jev_consequential as number | null | undefined;
  return (
    <div className="evidence">
      {review && (
        <p className="small">
          Governance review: <StatusBadge status={review.ok ? "ok" : "failed"} label={review.ok ? "passed" : "failed"} />
          {review.problems?.length ? <span className="warn-text"> {review.problems.join("; ")}</span> : null}
        </p>
      )}
      {policy && (
        <p className="small">
          Policy decision: <StatusBadge status={policy.decision} />{" "}
          {policy.reasons?.length ? <span className="muted">{policy.reasons.join(", ")}</span> : null}
        </p>
      )}
      {jev !== undefined && jev !== null && (
        <p className="small">JEV consequential-action probability: <strong>{fmtPct(jev)}</strong> <span className="muted">(escalates risk only; never grants)</span></p>
      )}
      <JsonView value={evidence} collapsed label="Full evidence" />
    </div>
  );
}

export function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: (a: Approval) => void }) {
  const [reason, setReason] = useState("");
  const act = useAction();
  const pending = approval.status === "pending";
  const expired = new Date(approval.expires_at).getTime() < Date.now();
  const decide = async (approve: boolean) => {
    const r = await act.run(() => (approve ? api.approve(approval.id, reason) : api.reject(approval.id, reason)));
    if (r) onDecided(r);
  };
  return (
    <article className={`approval ${pending ? "approval-pending" : ""}`}>
      <header className="approval-head">
        <strong>{ACTION_LABEL[approval.action] ?? approval.action}</strong>
        <StatusBadge status={approval.status} />
        <Tag tone={approval.risk_tier === "high" ? "danger" : approval.risk_tier === "medium" ? "warning" : "neutral"}>risk: {approval.risk_tier}</Tag>
      </header>
      <KeyValue items={[
        ["Destination", approval.destination ?? "—"],
        ["Payload hash", <code key="h" title={approval.payload_hash}>{shortHash(approval.payload_hash, 16)}</code>],
        ["Plan hash", <code key="p" title={approval.plan_hash ?? ""}>{shortHash(approval.plan_hash, 16)}</code>],
        ["Policy version", String(approval.policy_version)],
        ["Affected", approval.affected_assets.join(", ") || "—"],
        ["Expires", <span key="e" className={expired && pending ? "warn-text" : undefined}>{fmtDate(approval.expires_at)}{expired && pending ? " (expired)" : ""}</span>],
        ...(approval.decided_at ? [["Decided", `${fmtDate(approval.decided_at)}${approval.reason ? ` — ${approval.reason}` : ""}`] as [string, string]] : []),
      ]} />
      <GovernanceReview evidence={approval.evidence ?? {}} />
      {pending && (
        <div className="approval-actions">
          <label className="sr-only" htmlFor={`reason-${approval.id}`}>Reason</label>
          <input id={`reason-${approval.id}`} placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <button type="button" className="btn btn-success btn-sm" disabled={act.busy} onClick={() => decide(true)}>Approve</button>
          <button type="button" className="btn btn-danger btn-sm" disabled={act.busy} onClick={() => decide(false)}>Reject</button>
        </div>
      )}
      <p className="muted small">The approval binds to this exact payload hash; any change after approval invalidates it.</p>
      <ErrorBox error={act.error} />
    </article>
  );
}

export function ApprovalsPanel({ approvals, onDecided }: { approvals: Approval[]; onDecided: (a: Approval) => void }) {
  if (!approvals.length) return <EmptyState title="No approvals for this run" />;
  const sorted = [...approvals].sort((a, b) => (a.status === "pending" ? -1 : 0) - (b.status === "pending" ? -1 : 0));
  return <div className="approvals">{sorted.map((a) => <ApprovalCard key={a.id} approval={a} onDecided={onDecided} />)}</div>;
}
