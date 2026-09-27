import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { shortHash } from "../lib/format";
import { useAction } from "../lib/hooks";
import { to } from "../routes";
import { ErrorBox, Notice } from "./ui";

export interface ApprovalRequest {
  status: string;
  approval_id?: string;
  payload_hash?: string;
}

/**
 * A side effect in two calls (CLAUDE.md rule 5): the first asks for an approval bound to the payload's
 * hash, an approver decides it in the inbox, and the second call acts with the approved id (the
 * server re-verifies it immediately before acting). A pending or stale approval is refused with the
 * server's reason; nothing happens before the approval.
 */
export function ApprovalStepper<R extends ApprovalRequest>({ wsId, label, what, request, confirm, onDone, disabled, children }: {
  wsId: string;
  label: string;
  /** What the approval is for, in plain words. */
  what: string;
  request: () => Promise<R>;
  confirm: (approvalId: string, requested: R) => Promise<R>;
  onDone: (result: R) => void;
  disabled?: boolean;
  children?: ReactNode;
}) {
  const [requested, setRequested] = useState<R | null>(null);
  const act = useAction();
  const start = async () => {
    const r = await act.run(request);
    if (!r) return;
    if (r.status === "approval_required" && r.approval_id) setRequested(r);
    else onDone(r);
  };
  const go = async () => {
    if (!requested?.approval_id) return;
    const r = await act.run(() => confirm(requested.approval_id!, requested));
    if (r) {
      setRequested(null);
      onDone(r);
    }
  };
  return (
    <div className="stack approval-stepper">
      {!requested && (
        <div className="chip-row">
          <button type="button" className="btn btn-sm btn-primary" onClick={() => void start()} disabled={disabled || act.busy}>{act.busy ? "Requesting…" : label}</button>
          {children}
        </div>
      )}
      {requested && (
        <div className="stack" role="status" aria-label={`Approval for ${what}`}>
          <Notice tone="warning">Approval requested for {what}: an approver decides it in the <Link to={to.approvals(wsId)}>approvals inbox</Link>.
            It is bound to payload <code>{shortHash(requested.payload_hash ?? requested.approval_id, 10)}</code>; if anything it binds changes, it no longer
            executes and a new request is needed.</Notice>
          <div className="chip-row">
            <button type="button" className="btn btn-sm btn-primary" onClick={() => void go()} disabled={act.busy}>Continue with the approved request</button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => { setRequested(null); act.clear(); }}>Later</button>
          </div>
        </div>
      )}
      <ErrorBox error={act.error} />
    </div>
  );
}
