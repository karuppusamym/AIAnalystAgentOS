/**
 * The workspace-context part of the shared mock backend (Stream D): the context export (a download), the
 * purposes the preview knows, the "what the agents see" preview and the shared context-cache view. Stateless:
 * the preview echoes the purpose, question and scope it was asked for.
 */
import type { ContextCacheStats, ContextPreview, ContextPurpose } from "../api";

type Reply = { status: number; body: unknown; contentType?: string };

export const CONTEXT_PURPOSES: ContextPurpose[] = [
  { purpose: "sql_generation", label: "Writing SQL for Ask", description: "What the model sees when Ask writes new SQL for a question." },
  { purpose: "hypothesis_generation", label: "Proposing hypotheses", description: "What the investigator sees when it proposes what to test." },
];

const TABLE = "sn.incident";

export function contextPreview(purpose: string, question: string, sourceId: string | null): ContextPreview {
  const preamble = `Workspace: IT operations\nOBJECTIVE: ${question}\n<untrusted_context>\nCATALOG\nTABLE ${TABLE} [fact, rows≈1,200]\n  priority (text, categorical)\nGLOSSARY: NO_MATCH\n</untrusted_context>`;
  const volatile = JSON.stringify({ question });
  return {
    purpose, label: CONTEXT_PURPOSES.find((p) => p.purpose === purpose)?.label ?? purpose, question,
    scope: { assets: [TABLE], source_ids: ["src_sn"], denied_columns: 1, source_id: sourceId },
    system_prompt: "sql_generation.v1", system_text: "You are the SQL Engineer Agent.", preamble_text: preamble, volatile_text: volatile,
    stable_keys: ["objective", "catalog", "glossary"], omitted: [], no_match: ["glossary"], refused: null, trimmed: false, receipts: 1,
    estimated_tokens: { stable: 1840, volatile: 12, total: 1852 }, budget_chars: 24000,
    cache: { key: "aos:ctx:ws_demo:retrieval:abc", kind: "retrieval", hit: true, shared: true, enabled: true, ttl_seconds: 900 },
    knowledge_version: "kv0123456789abcdef",
    sections: [
      { name: "system", part: "stable", items: 1, chars: 1200, no_match: false },
      { name: "catalog", part: "stable", items: 1, chars: 5400, no_match: false },
      { name: "glossary", part: "stable", items: 0, chars: 10, no_match: true },
      { name: "question", part: "volatile", items: 1, chars: 44, no_match: false },
    ],
  };
}

export const CONTEXT_CACHE: ContextCacheStats = {
  workspace_id: "ws_demo", shared: true, enabled: true, ttl_seconds: 900, entries: { compiled: 1, retrieval: 2 }, total_entries: 3,
  by_kind: { retrieval: { sql_generation: { hits: 4, misses: 2, chars_reused: 12000 } } }, totals: { hits: 4, misses: 2, chars_reused: 12000 },
};

/** Route a context request; null when the path is not one. */
export function contextRoute(m: string, p: string, url: URL, ws: string): Reply | null {
  const W = `/workspaces/${ws}/context`;
  if (!p.startsWith(W)) return null;
  const rest = p.slice(W.length);
  const q = url.searchParams;
  if (m === "GET" && rest === "/export") {
    const format = q.get("format") ?? "okf";
    if (format === "json") return { status: 200, body: JSON.stringify({ format: "analystos.context/v1" }), contentType: "application/json" };
    return { status: 200, body: format === "okf" ? "PK-mock" : "# IT operations — data context\n", contentType: format === "okf" ? "application/zip" : "text/markdown" };
  }
  if (m === "GET" && rest === "/purposes") return { status: 200, body: CONTEXT_PURPOSES };
  if (m === "GET" && rest === "/preview") {
    const preview = contextPreview(q.get("purpose") ?? "sql_generation", q.get("question") || "What changed recently, and why?", q.get("source_id"));
    if (q.get("download")) return { status: 200, body: `AnalystOS context preview\n${preview.preamble_text}\n`, contentType: "text/plain" };
    return { status: 200, body: preview };
  }
  if (m === "GET" && rest === "/cache") return { status: 200, body: CONTEXT_CACHE };
  if (m === "DELETE" && rest === "/cache") return { status: 200, body: { cleared: CONTEXT_CACHE.total_entries } };
  return null;
}
