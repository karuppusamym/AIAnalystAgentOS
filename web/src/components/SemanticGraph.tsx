import { useMemo, useState } from "react";
import * as echarts from "echarts/core";
import { GraphChart } from "echarts/charts";
import { LabelLayout } from "echarts/features";
import { api, type KnowledgeGraph } from "../api";
import { DARK, LIGHT } from "../lib/charts";
import { filterGraph, graphOption, NODE_KINDS, NODE_LABELS } from "../lib/knowledge";
import { useAsync, usePrefersDark } from "../lib/hooks";
import { canvasSupported, EChart } from "./Chart";
import { Card, EmptyState, ErrorBox, Loading, Tag } from "./ui";

echarts.use([GraphChart, LabelLayout]);

function LineSample({ dashed }: { dashed: boolean }) {
  return (
    <svg width="36" height="10" aria-hidden="true" className="line-sample">
      <line x1="1" y1="5" x2="35" y2="5" stroke="currentColor" strokeWidth={dashed ? 1.5 : 2.5} strokeDasharray={dashed ? "5 4" : undefined} />
    </svg>
  );
}

/**
 * Knowledge → Semantic graph (P4-U04): tables, datasets, metrics, documents and pending AI
 * suggestions. Governed edges (approved model and metrics, validated or user-declared joins, links
 * and mappings of human-reviewed documents) are solid; inferred ones (proposed, discovered,
 * unreviewed, suggested) are dashed. The same edges are listed as a table for keyboard and screen
 * reader users, and wherever a canvas is unavailable.
 */
export function SemanticGraph({ wsId }: { wsId: string }) {
  const graph = useAsync(() => api.knowledgeGraph(wsId), [wsId]);
  const [kinds, setKinds] = useState<Set<string>>(new Set(NODE_KINDS));
  const [showInferred, setShowInferred] = useState(true);
  const dark = usePrefersDark();
  const shown = useMemo(() => (graph.data ? filterGraph(graph.data, kinds, showInferred) : null), [graph.data, kinds, showInferred]);
  const option = useMemo(() => (shown ? graphOption(shown, dark ? DARK : LIGHT) : null), [shown, dark]);

  if (graph.error) return <ErrorBox error={graph.error} onRetry={graph.reload} />;
  if (!graph.data || !shown || !option) return <Loading label="Building the semantic graph…" />;
  return (
    <div className="stack">
      <Card>
        <div className="graph-controls">
          <div className="chip-row" role="group" aria-label="Show node kinds">
            {NODE_KINDS.map((k) => (
              <label key={k} className="toggle small">
                <input type="checkbox" checked={kinds.has(k)} onChange={(e) => setKinds((s) => {
                  const n = new Set(s);
                  if (e.target.checked) n.add(k);
                  else n.delete(k);
                  return n;
                })} />
                {NODE_LABELS[k]} ({graph.data!.nodes.filter((n) => n.kind === k).length})
              </label>
            ))}
          </div>
          <label className="toggle small">
            <input type="checkbox" checked={showInferred} onChange={(e) => setShowInferred(e.target.checked)} /> Show inferred edges
          </label>
        </div>
        <ul className="graph-legend small" aria-label="Edge legend">
          <li><LineSample dashed={false} /> Governed ({shown.governed}): approved, validated or reviewed by a person</li>
          <li><LineSample dashed /> Inferred ({shown.inferred}): proposed, discovered, unreviewed or an AI suggestion</li>
        </ul>
        {graph.data.truncated && <p className="small muted">The graph is truncated to its first 400 nodes.</p>}
      </Card>
      {shown.nodes.length === 0 ? <EmptyState title="Nothing to draw">Crawl sources, approve KPIs or write knowledge documents.</EmptyState> : (
        canvasSupported() && (
          <EChart option={option} height={Math.min(760, Math.max(460, 60 + shown.nodes.length * 9))}
            label={`Semantic graph: ${shown.nodes.length} nodes, ${shown.governed} governed and ${shown.inferred} inferred edges`} />
        )
      )}
      <EdgeTable graph={shown} open={!canvasSupported()} />
    </div>
  );
}

function EdgeTable({ graph, open }: { graph: KnowledgeGraph; open: boolean }) {
  const label = new Map(graph.nodes.map((n) => [n.id, n.label]));
  return (
    <details className="graph-edges" open={open}>
      <summary>Edges as a table ({graph.edges.length})</summary>
      <div className="table-wrap">
        <table className="table table-compact">
          <caption className="sr-only">Semantic graph edges</caption>
          <thead><tr><th scope="col">From</th><th scope="col">Relation</th><th scope="col">To</th><th scope="col">Edge</th><th scope="col">Why</th></tr></thead>
          <tbody>
            {graph.edges.map((e, i) => (
              <tr key={i} data-governed={e.governed}>
                <td>{label.get(e.source) ?? e.source}</td>
                <td className="small">{e.kind}{e.label ? <span className="muted"> · {e.label}</span> : null}</td>
                <td>{label.get(e.target) ?? e.target}</td>
                <td>{e.governed ? <Tag tone="success">governed</Tag> : <Tag tone="warning">inferred</Tag>}</td>
                <td className="small muted">{e.why}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
