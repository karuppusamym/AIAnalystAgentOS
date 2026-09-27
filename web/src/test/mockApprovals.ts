/**
 * Hash-bound approvals the wave-2 mocks request (model promotion and rollback, scoring, pipeline
 * materialization). They appear in the approval inbox like any other and are decided there.
 */
import type { Approval } from "../api";

const T = "2026-09-26T11:00:00Z";
let pending: Record<string, Approval> = {};

export function resetApprovals(): void {
  pending = {};
}

export function requestApproval(id: string, action: string, destination: string, payload: Record<string, unknown>, assets: string[] = []): Approval {
  const a: Approval = {
    id, workspace_id: "ws_demo", run_id: null, action, risk_tier: "high", destination, affected_assets: assets,
    payload_hash: `ph_${id}_0123456789abcdef`, plan_hash: null, policy_version: 2, requested_by: "usr_analyst", status: "pending",
    decided_by: null, decided_at: null, reason: null, expires_at: "2099-01-01T00:00:00Z", evidence: {}, payload, created_at: T,
  };
  pending[id] = pending[id]?.status === "approved" ? pending[id] : a;
  return pending[id];
}

export const approvalStatus = (id: string | undefined | null): string | null => (id && pending[id] ? pending[id].status : null);

/** Decide one (the inbox's approve/reject); null when the id is not a wave-2 approval. */
export function decide(id: string, decision: "approve" | "reject", reason?: string | null): Approval | null {
  const a = pending[id];
  if (!a) return null;
  pending[id] = { ...a, status: decision === "approve" ? "approved" : "rejected", decided_by: "usr_approver", decided_at: T, reason: reason ?? null };
  return pending[id];
}

export function wave2Approvals(): Approval[] {
  return Object.values(pending);
}
