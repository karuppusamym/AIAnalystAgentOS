/**
 * Platform-settings editor helpers (PUT /api/admin/settings, services/platform_settings.py). Pure functions.
 *
 * The server deep-merges a patch into the current document EXCEPT for the maps below, which it
 * replaces wholesale (that is how a key is removed). So a change to one purpose's mode must send
 * the complete purpose_modes map, never a single-key map.
 */
import type { Dict, LLMMode, PlatformSettings, TokenSavingsRow } from "../api";

export const REPLACED_MAPS = new Set(["purpose_modes", "routing_overrides", "profile_models"]);
export const LLM_MODES: LLMMode[] = ["off", "auto", "always"];
export const MODE_TEXT: Record<LLMMode, string> = {
  off: "never call a model (rule path only)",
  auto: "rules first; call the model only when they are insufficient",
  always: "call the model whenever one is available",
};

/** Sections whose scalar values (numbers, booleans, enums) the editor exposes as limits. */
export const LIMIT_SECTIONS = ["llm", "analysis", "crawl", "monitors", "sources"] as const;
export const SECTION_LABELS: Record<string, string> = {
  llm: "Model calls", analysis: "Analysis", crawl: "Metadata crawler", monitors: "Monitors", sources: "Sources", features: "Features",
};

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const isObj = (v: unknown): v is Dict => !!v && typeof v === "object" && !Array.isArray(v);

/** The minimal PUT patch turning `original` into `draft` (replaced maps are sent whole). */
export function settingsPatch(original: PlatformSettings, draft: PlatformSettings): Dict {
  const patch: Dict = {};
  for (const section of Object.keys(draft)) {
    const a = (original as Dict)[section];
    const b = (draft as Dict)[section];
    if (!isObj(a) || !isObj(b)) {
      if (!same(a, b)) patch[section] = b;
      continue;
    }
    const sp: Dict = {};
    for (const key of new Set([...Object.keys(a), ...Object.keys(b)])) {
      if (!same(a[key], b[key])) sp[key] = b[key] ?? (REPLACED_MAPS.has(key) ? {} : null);
    }
    if (Object.keys(sp).length) patch[section] = sp;
  }
  return patch;
}

/** Changed leaf paths for the "pending changes" list, e.g. llm.purpose_modes.planning: always → auto. */
export function pendingChanges(original: PlatformSettings, draft: PlatformSettings): { path: string; from: unknown; to: unknown }[] {
  const out: { path: string; from: unknown; to: unknown }[] = [];
  const walk = (a: unknown, b: unknown, path: string) => {
    if (isObj(a) || isObj(b)) {
      const ao = isObj(a) ? a : {};
      const bo = isObj(b) ? b : {};
      for (const k of [...new Set([...Object.keys(ao), ...Object.keys(bo)])].sort()) walk(ao[k], bo[k], path ? `${path}.${k}` : k);
      return;
    }
    if (!same(a, b)) out.push({ path, from: a, to: b });
  };
  walk(original, draft, "");
  return out;
}

/** Effective mode of a purpose: a missing key means "always". */
export function modeOf(modes: Record<string, LLMMode> | undefined, purpose: string): LLMMode {
  return modes?.[purpose] ?? "always";
}

/**
 * Set one purpose's mode in a copy of the full map. Choosing "always" for a purpose that was not in
 * the saved map removes the key again, so an edit-and-revert produces no change.
 */
export function withPurposeMode(saved: Record<string, LLMMode>, current: Record<string, LLMMode>, purpose: string,
  mode: LLMMode): Record<string, LLMMode> {
  const next = { ...current };
  if (mode === "always" && !(purpose in saved)) delete next[purpose];
  else next[purpose] = mode;
  return next;
}

/** Purposes whose effective mode a preset would change (a preset replaces purpose_modes wholesale). */
export function presetEffect(current: Record<string, LLMMode>, preset: Record<string, LLMMode>, purposes: string[]):
  { purpose: string; from: LLMMode; to: LLMMode }[] {
  const all = [...new Set([...purposes, ...Object.keys(current), ...Object.keys(preset)])].sort();
  return all.map((p) => ({ purpose: p, from: modeOf(current, p), to: modeOf(preset, p) })).filter((x) => x.from !== x.to);
}

export interface FieldSchema {
  type?: string;
  minimum?: number;
  maximum?: number;
  enum?: string[];
  title?: string;
}

/** JSON-schema facts for settings[section][key] from PlatformSettings.model_json_schema(). */
export function fieldSchema(schema: Dict | undefined, section: string, key: string): FieldSchema {
  if (!schema) return {};
  const defs = (schema.$defs ?? schema.definitions ?? {}) as Record<string, Dict>;
  const secProp = ((schema.properties ?? {}) as Record<string, Dict>)[section];
  const resolve = (node: Dict | undefined): Dict | undefined => {
    if (!node) return undefined;
    const ref = (node.$ref as string | undefined) ?? ((node.allOf as Dict[] | undefined)?.[0]?.$ref as string | undefined);
    return ref ? defs[ref.split("/").pop() ?? ""] : node;
  };
  const sec = resolve(secProp);
  const prop = resolve(((sec?.properties ?? {}) as Record<string, Dict>)[key]);
  if (!prop) return {};
  return {
    type: prop.type as string | undefined, minimum: prop.minimum as number | undefined, maximum: prop.maximum as number | undefined,
    enum: prop.enum as string[] | undefined, title: prop.title as string | undefined,
  };
}

/** Out-of-range or non-numeric limits, keyed "section.key". */
export function validateLimits(draft: PlatformSettings, schema: Dict | undefined): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const section of LIMIT_SECTIONS) {
    const values = (draft as Dict)[section];
    if (!isObj(values)) continue;
    for (const [key, v] of Object.entries(values)) {
      if (typeof v !== "number") continue;
      const fs = fieldSchema(schema, section, key);
      if (!Number.isFinite(v)) errors[`${section}.${key}`] = "Must be a number.";
      else if (fs.type === "integer" && !Number.isInteger(v)) errors[`${section}.${key}`] = "Must be a whole number.";
      else if (fs.minimum !== undefined && v < fs.minimum) errors[`${section}.${key}`] = `Must be ≥ ${fs.minimum}.`;
      else if (fs.maximum !== undefined && v > fs.maximum) errors[`${section}.${key}`] = `Must be ≤ ${fs.maximum}.`;
    }
  }
  return errors;
}

export function humanKey(key: string): string {
  return key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** Share of tokens avoided: saved / (used + saved). */
export function savedShare(row: Pick<TokenSavingsRow, "tokens_used" | "tokens_saved">): number {
  const denom = row.tokens_used + row.tokens_saved;
  return denom ? row.tokens_saved / denom : 0;
}

export const statusCount = (row: TokenSavingsRow, status: string): number => row.by_status?.[status] ?? 0;
