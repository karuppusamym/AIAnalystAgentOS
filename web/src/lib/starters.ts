/**
 * Starter questions and example SQL for Ask, built only from the catalog (roles, keys and measured
 * profiles) — no model call. A fresh workspace on any database gets questions about its own tables:
 * a total, a trend, a ranking and a breakdown, each short enough to read at a glance.
 */
import type { CatalogAsset, CatalogColumn } from "../api";

const SENSITIVE = new Set(["pii", "restricted", "sensitive"]);
const TIME_ROLES = new Set(["timestamp", "date"]);
const MEASURE_ROLES = new Set(["amount", "measure", "duration", "percent"]);
const DIMENSION_ROLES = new Set(["dimension", "code", "flag"]);

function usable(c: CatalogColumn): boolean {
  return !c.tags.some((t) => SENSITIVE.has(t)) && !c.pii?.category;
}

function words(c: CatalogColumn): string {
  const w = (c.business_name || c.name.replace(/_/g, " ")).toLowerCase();
  return c.role === "name" ? w.replace(/ name$/, "") : w;
}

/** Ticket numbers, codes of one row, surrogate keys: unique per row, never a grouping. */
const ID_LIKE = /^(number|.*_(number|no|nbr|num|id|key|uuid|guid|sk))$/i;

function plural(noun: string): string {
  if (/(s|x|ch|sh)$/.test(noun)) return `${noun}es`;
  if (/[^aeiou]y$/.test(noun)) return `${noun.slice(0, -1)}ies`;
  return `${noun}s`;
}

/** What one row of the table is, in plain words ("incident", "order line"). */
export function entityOf(a: CatalogAsset): string {
  const raw = (a.entity || a.business_name || a.name).replace(/^(fact|fct|dim|stg|raw)_/i, "").replace(/_/g, " ").trim().toLowerCase();
  return raw.replace(/ies$/, "y").replace(/(?<!s)s$/, "") || "row";
}

/** The table most questions are about: a fact or event table, else the largest selected one. */
export function primaryTable(assets: CatalogAsset[]): CatalogAsset | null {
  const pool = assets.filter((a) => a.selected && a.lifecycle === "active");
  if (!pool.length) return null;
  const rank = (a: CatalogAsset) => (a.role === "fact" ? 2 : a.role === "event" ? 1 : 0);
  return [...pool].sort((a, b) => rank(b) - rank(a) || (b.row_count ?? 0) - (a.row_count ?? 0))[0];
}

function timeColumn(a: CatalogAsset): CatalogColumn | undefined {
  const named = a.time_column ? a.columns.find((c) => c.name === a.time_column) : undefined;
  return named ?? a.columns.find((c) => usable(c) && (TIME_ROLES.has(c.role ?? "") || c.semantic_type === "datetime")
    && (c.profile?.null_rate ?? 0) < 0.5);
}

/**
 * Categorical columns worth grouping by: 2–30 distinct values, mostly filled, not a key. Without a
 * profile, names decide: identifier-like names are skipped, and a `<x>_name` column beside its
 * foreign key `<x>` (a denormalised label) counts as a dimension and comes first.
 */
function dimensions(a: CatalogAsset): CatalogColumn[] {
  const names = new Set(a.columns.map((c) => c.name));
  const labelOfKey = (c: CatalogColumn) => c.role === "name" && /_name$/.test(c.name) && names.has(c.name.replace(/_name$/, ""));
  return a.columns.filter((c) => {
    if (!usable(c) || c.is_key || c.role === "identifier" || c.role === "foreign_key") return false;
    if (c.profile?.distinct === undefined && ID_LIKE.test(c.name)) return false;
    if (labelOfKey(c)) return c.profile?.distinct === undefined || (c.profile.distinct >= 2 && c.profile.distinct <= 500);
    const d = c.profile?.distinct;
    const fitsCardinality = d === undefined ? DIMENSION_ROLES.has(c.role ?? "") : d >= 2 && d <= 30;
    return fitsCardinality && (DIMENSION_ROLES.has(c.role ?? "") || c.semantic_type === "categorical" || c.semantic_type === "boolean")
      && (c.profile?.null_rate ?? 0) < 0.5;
  }).sort((x, y) => score(y) - score(x) || (x.profile?.null_rate ?? 0) - (y.profile?.null_rate ?? 0));

  function score(c: CatalogColumn): number {
    return (labelOfKey(c) ? 2 : 0) + ((c.profile?.distinct ?? 0) <= 30 ? 1 : 0);
  }
}

/** Quantities worth averaging: the catalog role decides (a numeric code such as a priority is a dimension). */
function measures(a: CatalogAsset): CatalogColumn[] {
  const unclassified = (c: CatalogColumn) => !c.role || c.role === "unknown";
  return a.columns.filter((c) => usable(c) && !c.is_key && (MEASURE_ROLES.has(c.role ?? "") || (unclassified(c) && c.semantic_type === "numeric"))
    && c.role !== "identifier" && c.role !== "foreign_key" && (c.profile?.null_rate ?? 0) < 0.5);
}

export function starterQuestions(assets: CatalogAsset[], limit = 4): string[] {
  const a = primaryTable(assets);
  if (!a) return [];
  const one = entityOf(a);
  const many = plural(one);
  const t = timeColumn(a);
  const dims = dimensions(a);
  const meas = measures(a);
  const out: string[] = [`How many ${many} are there?`];
  if (t) out.push(`How many ${many} per month?`);
  if (dims[0]) out.push(`Which ${words(dims[0])} has the most ${many}?`);
  if (meas[0] && dims[0]) out.push(`What is the average ${words(meas[0])} by ${words(dims[0])}?`);
  else if (dims[1]) out.push(`How many ${many} by ${words(dims[1])}?`);
  return [...new Set(out)].slice(0, limit);
}

const quote = (name: string) => (/^[a-z_][a-z0-9_]*$/.test(name) ? name : `"${name.replace(/"/g, '""')}"`);

/** Read-only example statements for the SQL console, over the primary table's own columns. */
export function exampleSql(assets: CatalogAsset[]): { label: string; sql: string }[] {
  const a = primaryTable(assets);
  if (!a) return [];
  const many = plural(entityOf(a));
  const alias = `${many.replace(/[^a-z0-9]+/g, "_")}`.replace(/^_|_$/g, "") || "row_count";
  const out = [{ label: `Count ${many}`, sql: `SELECT COUNT(*) AS ${alias} FROM ${a.fq}` }];
  const dims = dimensions(a);
  for (const d of dims.slice(0, 2)) {
    out.push({ label: `${many[0].toUpperCase()}${many.slice(1)} by ${words(d)}`,
      sql: `SELECT ${quote(d.name)}, COUNT(*) AS ${alias} FROM ${a.fq} GROUP BY ${quote(d.name)} ORDER BY ${alias} DESC` });
  }
  const m = measures(a)[0];
  if (m && dims[0]) {
    out.push({ label: `Average ${words(m)} by ${words(dims[0])}`,
      sql: `SELECT ${quote(dims[0].name)}, AVG(${quote(m.name)}) AS avg_${m.name} FROM ${a.fq} GROUP BY ${quote(dims[0].name)} ORDER BY avg_${m.name} DESC` });
  }
  return out;
}
