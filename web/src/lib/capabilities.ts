/**
 * Registry page helpers (P4-U06/U07). The page is generated from GET /api/capabilities, so these
 * treat kinds, certification states and side effects as open sets: a plugin that brings a kind or
 * value this UI has never heard of still gets a group and a (neutral) badge, not a crash.
 */
import { api, ApiError, type CapabilityInvocation, type CapabilityManifest, type CapabilitySummary, type Dict } from "../api";
import type { Tone } from "./status";

/** Display order and labels for the kinds in contracts/capability.py; unknown kinds sort after, by name. */
export const KIND_ORDER: { kind: string; label: string }[] = [
  { kind: "Playbook", label: "Playbooks" },
  { kind: "Agent", label: "Agents" },
  { kind: "Method", label: "Analysis methods" },
  { kind: "Tool", label: "Tools" },
  { kind: "Skill", label: "Skills" },
  { kind: "DecisionPurpose", label: "Decision purposes" },
  { kind: "Connector", label: "Connectors" },
  { kind: "Engine", label: "Engines" },
  { kind: "Publisher", label: "Publishers" },
  { kind: "Detector", label: "Detectors" },
  { kind: "Crawler", label: "Crawlers" },
  { kind: "KnowledgePack", label: "Knowledge packs" },
  { kind: "Renderer", label: "Renderers" },
];

export function kindLabel(kind: string): string {
  return KIND_ORDER.find((k) => k.kind === kind)?.label ?? kind.replace(/([a-z])([A-Z])/g, "$1 $2");
}

export interface KindGroup {
  kind: string;
  label: string;
  items: CapabilitySummary[];
}

export function groupByKind(caps: CapabilitySummary[]): KindGroup[] {
  const by = new Map<string, CapabilitySummary[]>();
  for (const c of caps) by.set(c.kind, [...(by.get(c.kind) ?? []), c]);
  const rank = (k: string) => {
    const i = KIND_ORDER.findIndex((x) => x.kind === k);
    return i < 0 ? KIND_ORDER.length : i;
  };
  return [...by.entries()]
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([kind, items]) => ({ kind, label: kindLabel(kind), items: [...items].sort((a, b) => a.id.localeCompare(b.id)) }));
}

export function matches(c: CapabilitySummary, q: string): boolean {
  const n = q.trim().toLowerCase();
  if (!n) return true;
  return `${c.id} ${c.summary} ${c.kind} ${c.source} ${c.tags.join(" ")}`.toLowerCase().includes(n);
}

export const CERT_TONE: Record<string, Tone> = { certified: "success", tested: "info", draft: "neutral", deprecated: "warning" };
export const SIDE_EFFECT: Record<string, { tone: Tone; label: string; hint: string }> = {
  none: { tone: "neutral", label: "no side effect", hint: "Pure computation" },
  read_source: { tone: "info", label: "reads sources", hint: "Reads data through the query gateway" },
  write_internal: { tone: "warning", label: "writes internally", hint: "Writes platform records only" },
  write_external: { tone: "danger", label: "writes externally", hint: "Writes outside the platform: every use needs an approval" },
};

export function certTone(status: string | null | undefined): Tone {
  return CERT_TONE[status ?? ""] ?? "neutral";
}

/** Unknown side effects read as the most dangerous class, as on the server (contracts/capability.py). */
export function sideEffectInfo(s: string | null | undefined) {
  return SIDE_EFFECT[s ?? ""] ?? { tone: "danger" as Tone, label: s ? `${s} (unclassified)` : "unclassified", hint: "Treated as writing externally" };
}

/** Governed by source registration, policy packs or the MCP allowlist; the per-workspace toggle does not apply. */
export function selfGoverned(c: { kind: string; source?: string }): boolean {
  return c.kind === "Connector" || c.kind === "KnowledgePack" || (c.source ?? "").startsWith("mcp:");
}

/** `mcp://server/tool` → its parts (MCP tools are invoked through the MCP client route). */
export function mcpTarget(m: { entry?: string | null; spec?: Dict }): { server: string; tool: string } | null {
  const server = typeof m.spec?.server === "string" ? m.spec.server : null;
  const tool = typeof m.spec?.tool === "string" ? m.spec.tool : null;
  if (server && tool) return { server, tool };
  const match = /^mcp:\/\/([^/]+)\/(.+)$/.exec(m.entry ?? "");
  return match ? { server: match[1], tool: match[2] } : null;
}

/**
 * The manifest behind a registry row. Workspace MCP tools are an overlay the global detail route
 * does not know, so those come from the workspace's MCP capability list.
 */
export async function loadManifest(c: CapabilitySummary, ws: string | null): Promise<CapabilityManifest> {
  if (c.source.startsWith("mcp:") && ws) {
    const list = await api.mcpCapabilities(ws);
    const m = list.find((x) => x.id === c.id);
    if (m) return m;
  }
  return api.getCapability(c.id);
}

export type InvokeOutcome =
  | { state: "ok"; result: unknown; raw: CapabilityInvocation }
  | { state: "approval"; approvalId: string | null; raw: CapabilityInvocation }
  | { state: "error"; message: string; raw: CapabilityInvocation }
  | { state: "unsupported"; message: string };

/** Run a capability with form arguments. Side effects never bypass approvals: the server decides. */
export async function invoke(m: CapabilityManifest, ws: string, args: Dict): Promise<InvokeOutcome> {
  let raw: CapabilityInvocation;
  try {
    const target = mcpTarget(m);
    raw = target ? await api.invokeMcpTool(ws, target.server, target.tool, args) : await api.invokeCapability(ws, m.id, args);
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 405) && !mcpTarget(m)) {
      return { state: "unsupported", message: "This server has no route to run built-in or plugin capabilities directly yet; they run inside investigations." };
    }
    throw err;
  }
  if (raw.status === "approval_required") return { state: "approval", approvalId: raw.approval_id ?? null, raw };
  if (raw.status === "error") return { state: "error", message: String(raw.error ?? "The capability reported an error."), raw };
  return { state: "ok", result: "result" in raw ? raw.result : raw, raw };
}
