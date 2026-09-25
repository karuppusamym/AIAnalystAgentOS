import { Link, useParams } from "react-router-dom";
import { to } from "../routes";
import { api } from "../api";
import { EmptyState, ErrorBox, Loading, PageHeader, StatusBadge, Tag, Value } from "../components/ui";
import { durationBetween, fmtDate } from "../lib/format";
import { useAsync } from "../lib/hooks";

export function RunsPage() {
  const { wsId = "" } = useParams();
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);
  return (
    <div className="page">
      <PageHeader title="Investigations" subtitle="Every investigation is planned, executed by agents under policy, verified, and auditable."
        actions={<Link to={to.workspace(wsId)} className="btn btn-primary">Start analysis</Link>} />
      <ErrorBox error={runs.error} onRetry={runs.reload} />
      {runs.loading && !runs.data && <Loading />}
      {runs.data?.length === 0 && <EmptyState title="No runs yet">Start an analysis from the workspace home.</EmptyState>}
      {!!runs.data?.length && (
        <div className="table-wrap card">
          <table className="table">
            <thead>
              <tr><th>Objective</th><th>Status</th><th>Autonomy</th><th>Started</th><th>Duration</th><th className="num">Tokens</th><th className="num">Cost</th><th><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id}>
                  <td><Link to={to.run(wsId, r.id)} className="clamp-2">{r.objective}</Link><div className="muted small"><code>{r.id}</code>
                    {r.origin?.type === "schedule" && <> <Tag tone="info">scheduled</Tag></>}
                    {r.origin?.type === "alert" && <> <Tag tone="warning">alert investigation</Tag></>}</div></td>
                  <td><StatusBadge status={r.status} /></td>
                  <td>L{r.autonomy_level}</td>
                  <td className="small">{fmtDate(r.started_at ?? r.created_at)}</td>
                  <td className="small">{durationBetween(r.started_at, r.finished_at)}</td>
                  <td className="num"><Value value={r.tokens} format="int" /></td>
                  <td className="num"><Value value={r.cost_usd} format="usd" /></td>
                  <td><Link to={to.runConsole(wsId, r.id)} className="btn btn-xs btn-ghost">Console</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
