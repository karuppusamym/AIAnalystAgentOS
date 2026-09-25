import type { Lineage } from "../api";
import { layerLineage, nodeKey } from "../lib/lineage";
import { EmptyState } from "./ui";

/** Layered lineage: upstream on the left, downstream on the right, plus the explicit edge list. */
export function LineageGraph({ lineage, focus, onSelect }: {
  lineage: Lineage | null | undefined;
  focus?: { type: string; id: string };
  onSelect?: (type: string, id: string) => void;
}) {
  if (!lineage || lineage.nodes.length === 0) return <EmptyState title="No lineage recorded" />;
  const layers = layerLineage(lineage);
  const focusKey = focus ? nodeKey(focus.type, focus.id) : null;
  return (
    <div className="lineage">
      <div className="lineage-layers" role="list" aria-label="Lineage layers (upstream to downstream)">
        {layers.map((layer, i) => (
          <div key={i} className="lineage-layer" role="listitem">
            <div className="lineage-layer-label muted small">{i === 0 ? "upstream" : `step ${i}`}</div>
            {layer.map((n) => {
              const selectable = onSelect && (n.type === "chart" || n.type === "dashboard" || n.type === "dataset"
                || n.type === "artifact" || n.type === "metric");
              return (
                <button type="button" key={n.key} disabled={!selectable}
                  className={`lineage-node node-${n.type} ${n.key === focusKey ? "lineage-focus" : ""}`}
                  onClick={() => selectable && onSelect?.(n.type, n.id)} title={`${n.type} ${n.id}`}>
                  <span className="lineage-type">{n.type}</span>
                  <span className="lineage-id">{n.id}</span>
                </button>
              );
            })}
          </div>
        ))}
      </div>
      <details className="lineage-edges">
        <summary>{lineage.edges.length} edges</summary>
        <ul>
          {lineage.edges.map((e, i) => (
            <li key={i}>
              <code>{e.from[0]}:{e.from[1]}</code> <span className="muted">— {e.relation} →</span> <code>{e.to[0]}:{e.to[1]}</code>
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
