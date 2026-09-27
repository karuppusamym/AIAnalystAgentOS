import { api, type DefinitionVersion } from "../api";

export interface AnalysisContextSpec {
  purpose: string;
  business_description: string;
  question_template: string;
  source_ids: string[];
  metric_names: string[];
}

export function contextSpec(row: DefinitionVersion): AnalysisContextSpec {
  const s = row.spec ?? {};
  return {
    purpose: String(s.purpose ?? ""),
    business_description: String(s.business_description ?? ""),
    question_template: String(s.question_template ?? ""),
    source_ids: Array.isArray(s.source_ids) ? s.source_ids.map(String) : [],
    metric_names: Array.isArray(s.metric_names) ? s.metric_names.map(String) : [],
  };
}

export async function analysisContexts(wsId: string): Promise<DefinitionVersion[]> {
  const rows: DefinitionVersion[] = [];
  let cursor: string | null = null;
  do {
    const page = await api.listDefinitions(wsId, { kind: "analysis_context", ...(cursor ? { cursor } : {}) });
    rows.push(...await Promise.all(page.items.map((r) => api.getDefinition(wsId, r.id))));
    cursor = page.next_cursor;
  } while (cursor);
  return rows;
}
