import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Approval } from "../api";
import { ApprovalCard } from "../components/ApprovalsPanel";
import { EmptyState, ErrorBox, Loading, PageHeader, Tabs } from "../components/ui";
import { useAsync } from "../lib/hooks";
import { to } from "../routes";

type Filter = "pending" | "all";

/**
 * Operate → Approvals: the workspace inbox. Before increment 4 approvals were only reachable from
 * each run; this lists them across runs (GET …/approvals). Each card binds to its payload hash.
 */
export function ApprovalsPage() {
  const { wsId = "" } = useParams();
  const [filter, setFilter] = useState<Filter>("pending");
  const list = useAsync(() => api.listApprovals(wsId, filter === "pending" ? "pending" : undefined), [wsId, filter]);
  const [decided, setDecided] = useState<Record<string, Approval>>({});
  const rows = (list.data ?? []).map((a) => decided[a.id] ?? a);
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
            <ApprovalCard approval={a} onDecided={(d) => setDecided((m) => ({ ...m, [d.id]: d }))} />
            {a.run_id && <Link className="small" to={to.run(wsId, a.run_id)}>Open investigation <code>{a.run_id}</code></Link>}
          </div>
        ))}
      </div>
    </div>
  );
}
