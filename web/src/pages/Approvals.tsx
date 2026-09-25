import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Approval } from "../api";
import { ApprovalCard, previousApproved } from "../components/ApprovalsPanel";
import { EmptyState, ErrorBox, Loading, PageHeader, Tabs } from "../components/ui";
import { useAsync } from "../lib/hooks";
import { to } from "../routes";

type Filter = "pending" | "all";

/**
 * Operate → Approvals: the workspace inbox. Each card binds to its payload hash, plan hash and
 * policy version, and shows its payload against the last approved proposal for the same action
 * and destination (so the approver sees the change, not a blob). The whole history is loaded once
 * so the comparison works on the Pending tab too.
 */
export function ApprovalsPage() {
  const { wsId = "" } = useParams();
  const [filter, setFilter] = useState<Filter>("pending");
  const list = useAsync(() => api.listApprovals(wsId), [wsId]);
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const [decided, setDecided] = useState<Record<string, Approval>>({});
  const all = (list.data ?? []).map((a) => (decided[a.id] ? { ...a, ...decided[a.id], payload: a.payload } : a));
  const rows = filter === "pending" ? all.filter((a) => a.status === "pending") : all;
  return (
    <div className="page">
      <PageHeader title="Approvals"
        subtitle="Side effects (publish, schedule, export) wait here. Each approval is bound to a payload hash, plan hash and policy version." />
      <Tabs value={filter} onChange={setFilter} tabs={[{ id: "pending", label: "Pending" }, { id: "all", label: "All" }]} />
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data && rows.length === 0 && (
        <EmptyState title={filter === "pending" ? "Nothing waiting for approval" : "No approvals yet"}>
          Approvals are requested by investigations; see <Link to={to.investigations(wsId)}>Investigations</Link>.
        </EmptyState>
      )}
      <div className="approvals">
        {rows.map((a) => (
          <div key={a.id} className="stack">
            <ApprovalCard approval={a} previous={previousApproved(a, all)} currentPolicyVersion={ws.data?.policy_version}
              onDecided={(d) => setDecided((m) => ({ ...m, [d.id]: d }))} />
            {a.run_id && <Link className="small" to={to.run(wsId, a.run_id)}>Open investigation <code>{a.run_id}</code></Link>}
          </div>
        ))}
      </div>
    </div>
  );
}
