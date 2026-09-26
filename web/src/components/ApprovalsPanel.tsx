import { useState } from "react";
import { api, type Approval } from "../api";
import { diffJson, outline, preview } from "../lib/diff";
import { fmtDate, fmtPct, shortHash } from "../lib/format";
import { useAction } from "../lib/hooks";
import { EmptyState, ErrorBox, KeyValue, StatusBadge, Tag, TechnicalDetails } from "./ui";

const ACTION_LABEL: Record<string, string> = {
  publish_dashboard: "Publish dashboards",
  execute_plan: "Execute analysis plan",
  mcp_tool_call: "Call an external MCP tool",
  elt_build: "Build with dbt (write to a target schema)",
  "semantic_metric.approve": "Approve a KPI definition",
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
      <TechnicalDetails value={evidence} label="Full evidence" />
    </div>
  );
}

/**
 * What the approver signs: the payload against the last approved proposal for the same action and
 * destination, path by path. A first proposal lists its contents instead.
 */
export function PayloadDiff({ approval, previous }: { approval: Approval; previous?: Approval | null }) {
  if (!approval.payload) return <p className="muted small">The payload is not included in this view; its hash is above.</p>;
  if (!previous?.payload) {
    const items = outline(approval.payload);
    return (
      <div className="payload-diff">
        <p className="small">Nothing was approved before for this action and destination. The proposal contains:</p>
        {items.length ? <ul className="small">{items.map((i) => <li key={i.key}><code>{i.key}</code> {i.summary}</li>)}</ul> : <p className="muted small">An empty payload.</p>}
      </div>
    );
  }
  const changes = diffJson(previous.payload, approval.payload);
  return (
    <div className="payload-diff">
      <p className="small">
        Compared with the last approved proposal <code title={previous.payload_hash}>{shortHash(previous.payload_hash, 12)}</code> ({fmtDate(previous.created_at)}):{" "}
        {changes.length === 0 ? <strong>identical content</strong> : <strong>{changes.length} change{changes.length > 1 ? "s" : ""}</strong>}
      </p>
      {changes.length > 0 && (
        <div className="table-wrap">
          <table className="table table-compact diff-table">
            <caption className="sr-only">Payload changes</caption>
            <thead><tr><th scope="col">Path</th><th scope="col">Change</th><th scope="col">Before</th><th scope="col">After</th></tr></thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.path} className={`diff-${c.kind}`}>
                  <td><code>{c.path}</code></td>
                  <td><Tag tone={c.kind === "added" ? "success" : c.kind === "removed" ? "danger" : "warning"}>{c.kind}</Tag></td>
                  <td className="small diff-before">{preview(c.before)}</td>
                  <td className="small diff-after">{preview(c.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function ApprovalCard({ approval, onDecided, previous, currentPolicyVersion }: {
  approval: Approval; onDecided: (a: Approval) => void; previous?: Approval | null; currentPolicyVersion?: number | null;
}) {
  const [reason, setReason] = useState("");
  const act = useAction();
  const pending = approval.status === "pending";
  const expired = new Date(approval.expires_at).getTime() < Date.now();
  const policyMoved = pending && typeof currentPolicyVersion === "number" && currentPolicyVersion !== approval.policy_version;
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
        ["Policy version", <span key="v">v{approval.policy_version}{typeof currentPolicyVersion === "number" && <span className="muted"> (current v{currentPolicyVersion})</span>}</span>],
        ["Affected", approval.affected_assets.join(", ") || "—"],
        ["Expires", <span key="e" className={expired && pending ? "warn-text" : undefined}>{fmtDate(approval.expires_at)}{expired && pending ? " (expired)" : ""}</span>],
        ...(approval.decided_at ? [["Decided", `${fmtDate(approval.decided_at)}${approval.reason ? ` — ${approval.reason}` : ""}`] as [string, string]] : []),
      ]} />
      {policyMoved && (
        <p className="small warn-text" role="status">The workspace policy changed since this was requested; execution re-verifies against the current policy and will refuse a mismatch.</p>
      )}
      <PayloadDiff approval={approval} previous={previous} />
      <GovernanceReview evidence={approval.evidence ?? {}} />
      {pending && (
        <div className="approval-actions">
          <label className="sr-only" htmlFor={`reason-${approval.id}`}>Reason</label>
          <input id={`reason-${approval.id}`} placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <button type="button" className="btn btn-success btn-sm" disabled={act.busy} onClick={() => decide(true)}>Approve</button>
          <button type="button" className="btn btn-danger btn-sm" disabled={act.busy} onClick={() => decide(false)}>Reject</button>
        </div>
      )}
      <p className="muted small">The approval binds to this exact payload hash, plan hash and policy version; any change after approval invalidates it.</p>
      <ErrorBox error={act.error} />
    </article>
  );
}

/** The last approved (or executed) proposal of the same action and destination before this one. */
export function previousApproved(a: Approval, all: Approval[]): Approval | null {
  const prior = all.filter((x) => x.id !== a.id && x.action === a.action && x.destination === a.destination
    && (x.status === "approved" || x.status === "executed") && x.created_at < a.created_at);
  prior.sort((x, y) => y.created_at.localeCompare(x.created_at));
  return prior[0] ?? null;
}

export function ApprovalsPanel({ approvals, onDecided }: { approvals: Approval[]; onDecided: (a: Approval) => void }) {
  if (!approvals.length) return <EmptyState title="No approvals for this run" />;
  const sorted = [...approvals].sort((a, b) => (a.status === "pending" ? -1 : 0) - (b.status === "pending" ? -1 : 0));
  return <div className="approvals">{sorted.map((a) => <ApprovalCard key={a.id} approval={a} onDecided={onDecided} previous={previousApproved(a, approvals)} />)}</div>;
}
