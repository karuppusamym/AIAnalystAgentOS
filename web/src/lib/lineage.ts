import type { Lineage } from "../api";

export const nodeKey = (type: string, id: string) => `${type}:${id}`;

/**
 * Assign each lineage node to a layer (longest path from a root along edge direction), so the
 * graph can be drawn left-to-right as upstream -> downstream. Cycles are broken by visit order.
 */
export function layerLineage(lin: Lineage): { type: string; id: string; key: string }[][] {
  const keys = lin.nodes.map((n) => nodeKey(n.type, n.id));
  const incoming = new Map<string, string[]>(keys.map((k) => [k, []]));
  for (const e of lin.edges) {
    const from = nodeKey(e.from[0], e.from[1]);
    const to = nodeKey(e.to[0], e.to[1]);
    if (!incoming.has(to)) incoming.set(to, []);
    if (!incoming.has(from)) incoming.set(from, []);
    incoming.get(to)!.push(from);
  }
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const visit = (k: string): number => {
    if (depth.has(k)) return depth.get(k)!;
    if (visiting.has(k)) return 0;
    visiting.add(k);
    const d = Math.max(-1, ...(incoming.get(k) ?? []).map(visit)) + 1;
    visiting.delete(k);
    depth.set(k, d);
    return d;
  };
  const all = [...incoming.keys()];
  all.forEach(visit);
  const layers: { type: string; id: string; key: string }[][] = [];
  for (const k of all) {
    const d = depth.get(k) ?? 0;
    const idx = k.indexOf(":");
    (layers[d] ??= []).push({ type: k.slice(0, idx), id: k.slice(idx + 1), key: k });
  }
  return layers.filter(Boolean).map((l) => l.sort((a, b) => a.key.localeCompare(b.key)));
}
