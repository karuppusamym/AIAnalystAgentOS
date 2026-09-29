import { api } from "../api";
import { useAsync } from "../lib/hooks";
import { LineageGraph } from "./LineageGraph";
import { ErrorBox, Loading, Notice } from "./ui";

/** A run's provenance: every edge it recorded, from the objective and hypotheses through experiments,
 * queries and tables to findings, datasets, charts and reports. */
export function RunLineage({ wsId, runId }: { wsId: string; runId: string }) {
  const lineage = useAsync(() => api.runLineage(wsId, runId), [wsId, runId]);
  if (lineage.error) return <ErrorBox error={lineage.error} onRetry={() => void lineage.reload()} />;
  if (!lineage.data) return <Loading label="Loading lineage…" />;
  return (
    <>
      {lineage.data.truncated && <Notice tone="warning">Showing the first {lineage.data.edges.length} edges of this run.</Notice>}
      <LineageGraph lineage={lineage.data} focus={{ type: "run", id: runId }} />
    </>
  );
}
