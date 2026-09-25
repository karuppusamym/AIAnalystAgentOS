/**
 * Source-kind form helpers (GET /api/source-kinds, services/sources.register_source). Pure functions.
 *
 * The form is generated from each kind's required/optional field names; the server re-validates
 * everything (required fields, credentials never in config, admin-enabled kinds).
 */
import type { Dict, SourceKindInfo } from "../api";

export const CATEGORY_LABELS: Record<string, string> = {
  database: "Databases",
  warehouse: "Warehouses",
  lakehouse: "Lakehouses",
  engine: "Query engines",
  file: "Files",
  api: "Applications (API)",
};

const CATEGORY_ORDER = ["database", "warehouse", "lakehouse", "engine", "file", "api"];

/** Config keys that take a list (comma or newline separated in the form). */
export const LIST_FIELDS = new Set(["schemas", "include", "exclude", "tables"]);
/** Config keys that take a whole number. */
export const NUMBER_FIELDS = new Set(["port", "max_tables", "page_size", "max_rows", "connect_timeout", "row_count_cap"]);
/** Keys the server refuses in config: credentials always go through secret_ref. */
export const CREDENTIAL_KEYS = ["password", "secret", "token", "api_key", "private_key", "credentials_json"];

const FIELD_LABELS: Record<string, string> = {
  host: "Host", port: "Port", database: "Database", username: "Username", schemas: "Schemas", include: "Include tables",
  exclude: "Exclude tables", max_tables: "Max tables", sslmode: "SSL mode", connect_timeout: "Connect timeout (s)",
  account: "Account", schema: "Default schema", warehouse: "Warehouse", role: "Role", project: "Project", http_path: "HTTP path",
  catalog: "Catalog", http_scheme: "HTTP scheme", path: "Path", row_count_cap: "Row count cap", instance_url: "Instance URL",
  tables: "Tables", page_size: "Page size", max_rows: "Max rows", delimiter: "Delimiter", sheet: "Sheet",
};

export function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? field.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function fieldHint(field: string): string | undefined {
  if (LIST_FIELDS.has(field)) {
    return field === "include" || field === "exclude" ? "Comma separated glob patterns on schema.table or table, e.g. sales.*, *_tmp." : "Comma separated.";
  }
  return undefined;
}

export const splitList = (s: string): string[] => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);

export function groupKinds(kinds: SourceKindInfo[]): { category: string; label: string; kinds: SourceKindInfo[] }[] {
  const by = new Map<string, SourceKindInfo[]>();
  for (const k of kinds) by.set(k.category, [...(by.get(k.category) ?? []), k]);
  const cats = [...by.keys()].sort((a, b) => {
    const ia = CATEGORY_ORDER.indexOf(a);
    const ib = CATEGORY_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  return cats.map((c) => ({
    category: c, label: CATEGORY_LABELS[c] ?? c,
    kinds: [...(by.get(c) ?? [])].sort((a, b) => a.label.localeCompare(b.label)),
  }));
}

/** The kind the form opens on: the first enabled one, preferring the demo ServiceNow source. */
export function defaultKind(kinds: SourceKindInfo[]): string {
  const usable = kinds.filter((k) => k.enabled);
  return (usable.find((k) => k.kind === "servicenow") ?? usable[0] ?? kinds[0])?.kind ?? "";
}

/** Suggested secret reference, e.g. env:POSTGRES_PASSWORD. */
export function suggestedSecretRef(kind: SourceKindInfo): string {
  if (!kind.secret_field) return "";
  return `env:${kind.kind.toUpperCase()}_${kind.secret_field.toUpperCase()}`;
}

export const SECRET_REF_PATTERN = /^(env:[A-Za-z_][A-Za-z0-9_]*|file:\/.+)$/;

export interface SourceFormErrors {
  [field: string]: string;
}

/** Convert raw form strings to the config object the API expects; empty optional fields are omitted. */
export function buildSourceConfig(kind: SourceKindInfo, values: Record<string, string>, executionMode?: string): Dict {
  const config: Dict = {};
  for (const field of [...kind.required, ...kind.optional]) {
    const raw = (values[field] ?? "").trim();
    if (!raw) continue;
    if (LIST_FIELDS.has(field)) config[field] = splitList(raw);
    else if (NUMBER_FIELDS.has(field)) config[field] = Number(raw);
    else config[field] = raw;
  }
  if (executionMode && executionMode !== kind.execution_mode) config.execution_mode = executionMode;
  return config;
}

export function validateSourceForm(kind: SourceKindInfo, name: string, values: Record<string, string>, secretRef: string): SourceFormErrors {
  const errors: SourceFormErrors = {};
  if (!name.trim()) errors.name = "Name is required.";
  for (const f of kind.required) if (!(values[f] ?? "").trim()) errors[f] = `${fieldLabel(f)} is required.`;
  for (const f of [...kind.required, ...kind.optional]) {
    const raw = (values[f] ?? "").trim();
    if (raw && NUMBER_FIELDS.has(f) && !(Number.isInteger(Number(raw)) && Number(raw) > 0)) errors[f] = `${fieldLabel(f)} must be a positive whole number.`;
  }
  const ref = secretRef.trim();
  if (ref && !SECRET_REF_PATTERN.test(ref)) errors.secret_ref = "Use env:NAME or file:/path — never the secret itself.";
  return errors;
}

export function executionModeText(mode: string): string {
  return mode === "pushdown"
    ? "Pushdown: governed read-only queries run in the source itself."
    : "Staged: selected tables are copied into the analytics database and queried there.";
}
