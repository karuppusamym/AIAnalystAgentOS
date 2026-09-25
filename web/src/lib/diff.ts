/**
 * Structural diff of two JSON documents (approval payloads: dashboard bundles, tool arguments).
 * The approval inbox shows what an approver is actually signing: the change against what was last
 * approved for the same action and destination, path by path, rather than two JSON blobs.
 */

export type ChangeKind = "added" | "removed" | "changed";

export interface Change {
  path: string;
  kind: ChangeKind;
  before?: unknown;
  after?: unknown;
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** A stable key for array items that carry one (dashboards, charts, datasets have key/name/id). */
function itemKey(v: unknown): string | null {
  if (!isObj(v)) return null;
  for (const k of ["key", "id", "name", "slug", "title"]) if (typeof v[k] === "string" || typeof v[k] === "number") return `${k}=${String(v[k])}`;
  return null;
}

function walk(before: unknown, after: unknown, path: string, out: Change[], limit: number): void {
  if (out.length >= limit) return;
  if (JSON.stringify(before) === JSON.stringify(after)) return;
  if (isObj(before) && isObj(after)) {
    const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
    for (const k of keys) {
      const p = path ? `${path}.${k}` : k;
      if (!(k in before)) out.push({ path: p, kind: "added", after: after[k] });
      else if (!(k in after)) out.push({ path: p, kind: "removed", before: before[k] });
      else walk(before[k], after[k], p, out, limit);
      if (out.length >= limit) return;
    }
    return;
  }
  if (Array.isArray(before) && Array.isArray(after)) {
    const keyed = [...before, ...after].every((x) => itemKey(x) !== null);
    if (keyed) {
      const b = new Map(before.map((x) => [itemKey(x)!, x]));
      const a = new Map(after.map((x) => [itemKey(x)!, x]));
      for (const k of [...new Set([...b.keys(), ...a.keys()])]) {
        const p = `${path}[${k}]`;
        if (!b.has(k)) out.push({ path: p, kind: "added", after: a.get(k) });
        else if (!a.has(k)) out.push({ path: p, kind: "removed", before: b.get(k) });
        else walk(b.get(k), a.get(k), p, out, limit);
        if (out.length >= limit) return;
      }
      return;
    }
    const n = Math.max(before.length, after.length);
    for (let i = 0; i < n; i++) {
      const p = `${path}[${i}]`;
      if (i >= before.length) out.push({ path: p, kind: "added", after: after[i] });
      else if (i >= after.length) out.push({ path: p, kind: "removed", before: before[i] });
      else walk(before[i], after[i], p, out, limit);
      if (out.length >= limit) return;
    }
    return;
  }
  out.push({ path: path || "(root)", kind: "changed", before, after });
}

export function diffJson(before: unknown, after: unknown, limit = 200): Change[] {
  const out: Change[] = [];
  walk(before, after, "", out, limit);
  return out;
}

/** Short, single-line rendering of a value for a diff cell. */
export function preview(v: unknown, max = 80): string {
  if (v === undefined) return "";
  if (isObj(v)) {
    const k = itemKey(v);
    const n = Object.keys(v).length;
    return k ? `{${k}, ${n} fields}` : `{${n} fields}`;
  }
  if (Array.isArray(v)) return `[${v.length} items]`;
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** What a payload contains, for a first proposal with nothing to compare against. */
export function outline(payload: unknown): { key: string; summary: string }[] {
  if (!isObj(payload)) return [];
  return Object.entries(payload).map(([k, v]) => ({ key: k, summary: preview(v) }));
}
