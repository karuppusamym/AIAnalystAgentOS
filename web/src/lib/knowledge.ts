/**
 * Knowledge studio helpers (P4-U04): the editable shape of an OKF document's frontmatter, trust
 * labels, review-queue decisions and the ECharts option for the semantic graph. Pure functions, so
 * the rules the screen follows are tested without rendering it.
 */
import type {
  Dict, KnowledgeDocSummary, KnowledgeDocument, KnowledgeGraph, KnowledgeSuggestion, ReviewDecisionBody, SuggestionField,
  VerifiedEntry,
} from "../api";
import type { ChartPalette } from "./charts";

// ------------------------------------------------------------------------------------ trust
export const TRUST_TIERS: Record<string, { label: string; tone: string; title: string }> = {
  "human-reviewed": { label: "human-reviewed", tone: "success", title: "A person verified this document (OKF §5.3)" },
  "machine-confirmed": { label: "machine-confirmed", tone: "info", title: "Only a process verified this document" },
  unverified: { label: "unverified", tone: "neutral", title: "Nobody has verified this document yet" },
};

export function trustTier(tier: string | null | undefined) {
  return TRUST_TIERS[tier ?? "unverified"] ?? { label: tier ?? "unverified", tone: "neutral", title: tier ?? "" };
}

/** OKF §5.4 lifecycle values offered in the editor; an unknown existing value is kept as an option. */
export const DOC_STATUSES = ["draft", "stable", "deprecated"];

/** The trust and identity fields the editor exposes; everything else round-trips as JSON. */
const MANAGED = ["type", "title", "description", "status", "tags", "stale_after", "verified"] as const;

export interface DocDraft {
  path: string;
  type: string;
  title: string;
  description: string;
  status: string;
  tags: string;
  /** `YYYY-MM-DD` (date input) or empty. */
  staleAfter: string;
  /** Existing verification entries; unchecked ones are dropped on save. */
  verified: { entry: VerifiedEntry; keep: boolean }[];
  markReviewed: boolean;
  /** The rest of the frontmatter (extensions such as `analystos`) as JSON text. */
  other: string;
  body: string;
  reason: string;
}

export function emptyDraft(path = ""): DocDraft {
  return { path, type: "Note", title: "", description: "", status: "draft", tags: "", staleAfter: "", verified: [], markReviewed: false,
    other: "{}", body: "# Note\n\n", reason: "" };
}

export function draftFrom(doc: KnowledgeDocument): DocDraft {
  const fm = (doc.frontmatter ?? {}) as Dict;
  const other: Dict = {};
  for (const [k, v] of Object.entries(fm)) if (!(MANAGED as readonly string[]).includes(k)) other[k] = v;
  const stale = typeof fm.stale_after === "string" ? fm.stale_after.slice(0, 10) : "";
  return {
    path: doc.path, type: String(fm.type ?? ""), title: String(fm.title ?? ""), description: String(fm.description ?? ""),
    status: String(fm.status ?? "stable"), tags: Array.isArray(fm.tags) ? fm.tags.map(String).join(", ") : "", staleAfter: stale,
    verified: (doc.trust?.verified ?? []).map((entry) => ({ entry, keep: true })), markReviewed: false,
    other: JSON.stringify(other, null, 2), body: doc.body ?? "", reason: "",
  };
}

export interface DraftProblems {
  path?: string;
  type?: string;
  other?: string;
}

const SAFE_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

/** The checks the server repeats; shown before a round trip. */
export function draftProblems(d: DocDraft): DraftProblems {
  const out: DraftProblems = {};
  const segs = d.path.split("/");
  if (!d.path.endsWith(".md") || segs.some((s) => !SAFE_SEGMENT.test(s))) {
    out.path = "Use folder/name.md with letters, digits, dots, dashes and underscores.";
  } else if (["index.md", "log.md"].includes(segs[segs.length - 1])) {
    out.path = "index.md and log.md are reserved OKF files.";
  }
  if (!d.type.trim()) out.type = "Every OKF document needs a type.";
  try {
    const parsed: unknown = JSON.parse(d.other || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) out.other = "Must be a JSON object.";
  } catch (err) {
    out.other = `Not valid JSON: ${err instanceof Error ? err.message : String(err)}`;
  }
  return out;
}

/** The frontmatter a save sends: managed fields from the form, the rest from the JSON text. */
export function frontmatterOf(d: DocDraft): Dict {
  const fm: Dict = { ...(JSON.parse(d.other || "{}") as Dict), type: d.type.trim() };
  if (d.title.trim()) fm.title = d.title.trim();
  if (d.description.trim()) fm.description = d.description.trim();
  if (d.status) fm.status = d.status;
  const tags = d.tags.split(",").map((t) => t.trim()).filter(Boolean);
  if (tags.length) fm.tags = tags;
  if (d.staleAfter) fm.stale_after = `${d.staleAfter}T00:00:00+00:00`;
  const kept = d.verified.filter((v) => v.keep).map((v) => v.entry);
  if (kept.length) fm.verified = kept;
  return fm;
}

/** Folder -> documents, folders sorted, root files first. */
export function groupByFolder(docs: KnowledgeDocSummary[]): [string, KnowledgeDocSummary[]][] {
  const m = new Map<string, KnowledgeDocSummary[]>();
  for (const d of docs) {
    const i = d.path.lastIndexOf("/");
    const folder = i < 0 ? "" : d.path.slice(0, i);
    if (!m.has(folder)) m.set(folder, []);
    m.get(folder)!.push(d);
  }
  return [...m.entries()].sort((a, b) => (a[0] === "" ? -1 : b[0] === "" ? 1 : a[0].localeCompare(b[0])));
}

export function matchesDoc(d: KnowledgeDocSummary, q: string): boolean {
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hay = `${d.path} ${d.title} ${d.type ?? ""} ${(d.tags ?? []).join(" ")}`.toLowerCase();
  return terms.every((t) => hay.includes(t));
}

// ------------------------------------------------------------------------------------ review queue
/** The field that carries a draft's text (the server's `primary_field`). */
export function primaryField(kind: string): string {
  return kind === "attested_computation" ? "statement" : kind === "table_description" ? "description" : "body";
}

export function fieldText(f: SuggestionField | undefined): string {
  if (!f) return "";
  const v = f.value;
  if (Array.isArray(v)) return v.map(String).join(", ");
  if (v && typeof v === "object") return JSON.stringify(v);
  return v === null || v === undefined ? "" : String(v);
}

/** Editable fields: text and lists of text (a computation stays as proposed). */
export function editableFields(s: KnowledgeSuggestion): string[] {
  return Object.entries(s.fields).filter(([, f]) => typeof f.value === "string" || (Array.isArray(f.value) && f.value.every((x) => typeof x === "string")))
    .map(([k]) => k).sort((a, b) => (a === primaryField(s.kind) ? -1 : b === primaryField(s.kind) ? 1 : a.localeCompare(b)));
}

/** Edited values back to the field's type; unchanged fields are left out (they keep their provenance). */
export function editedFields(s: KnowledgeSuggestion, edits: Record<string, string>): Dict {
  const out: Dict = {};
  for (const [name, text] of Object.entries(edits)) {
    const f = s.fields[name];
    if (!f || text === fieldText(f)) continue;
    out[name] = Array.isArray(f.value) ? text.split(",").map((x) => x.trim()).filter(Boolean) : text;
  }
  return out;
}

export function batchDecisions(ids: string[], action: "approve" | "reject", reason?: string): ReviewDecisionBody[] {
  return ids.map((id) => (action === "reject" ? { id, action, reason: reason?.trim() || null } : { id, action }));
}

/** Provenance as short "key: value" pairs, most telling first. */
export function provenanceLine(p: Dict | undefined): [string, string][] {
  if (!p) return [];
  const order = ["source", "model", "purpose", "prompt_version", "crawl_run", "rule", "by", "approval_id", "insight_id", "feedback_id"];
  const rank = (k: string) => (order.includes(k) ? order.indexOf(k) : order.length);
  return Object.entries(p).filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
    .map(([k, v]) => [k.replace(/_/g, " "), String(v)]);
}

// ------------------------------------------------------------------------------------ graph
export const NODE_KINDS = ["table", "dataset", "metric", "document", "suggestion"] as const;
export const NODE_LABELS: Record<string, string> = {
  table: "Tables", dataset: "Datasets", metric: "Metrics", document: "Documents", suggestion: "AI suggestions",
};

export function filterGraph(g: KnowledgeGraph, kinds: Set<string>, showInferred: boolean): KnowledgeGraph {
  const nodes = g.nodes.filter((n) => kinds.has(n.kind));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = g.edges.filter((e) => ids.has(e.source) && ids.has(e.target) && (showInferred || e.governed));
  return { ...g, nodes, edges, governed: edges.filter((e) => e.governed).length, inferred: edges.filter((e) => !e.governed).length };
}

/** ECharts force graph: one category per node kind; governed edges solid, inferred dashed. */
export function graphOption(g: KnowledgeGraph, palette: ChartPalette): object {
  const cats = NODE_KINDS.map((k) => ({ name: NODE_LABELS[k] }));
  const degree = new Map<string, number>();
  for (const e of g.edges) {
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
  }
  return {
    color: palette.series,
    tooltip: { backgroundColor: palette.tooltipBg, textStyle: { color: palette.text } },
    legend: { data: cats.map((c) => c.name), textStyle: { color: palette.text }, top: 0 },
    series: [{
      type: "graph", layout: "force", roam: true, draggable: true, top: 36,
      force: { repulsion: 180, edgeLength: [60, 140], gravity: 0.08 },
      categories: cats,
      label: { show: true, position: "right", color: palette.text, fontSize: 11 },
      edgeSymbol: ["none", "arrow"], edgeSymbolSize: 6,
      lineStyle: { color: palette.textMuted, opacity: 0.8, width: 1.4 },
      data: g.nodes.map((n) => ({
        id: n.id, name: n.label, category: Math.max(0, NODE_KINDS.indexOf(n.kind as (typeof NODE_KINDS)[number])),
        symbolSize: 10 + Math.min(14, 2 * (degree.get(n.id) ?? 0)), symbol: n.kind === "suggestion" ? "diamond" : "circle",
        value: n.kind,
      })),
      links: g.edges.map((e) => ({
        source: e.source, target: e.target, value: e.why,
        lineStyle: { type: e.governed ? "solid" : "dashed", width: e.governed ? 2 : 1.2 },
      })),
    }],
  };
}
