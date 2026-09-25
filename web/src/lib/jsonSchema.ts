/**
 * The subset of JSON Schema the capability forms understand (spec v3 §9 "capability-driven UI").
 *
 * A manifest's `input_schema` becomes a form: object, string, number, integer, boolean, enum and
 * arrays of primitives, with required fields, descriptions, defaults and validation messages.
 * Anything richer (unions, nested arrays of objects) falls back to a JSON text field so a plugin is
 * never unusable, only less friendly. Pure functions: the form component holds a *draft* (strings
 * for numbers, so "4." can be typed) and these turn it into a value or a list of problems.
 */

export type Schema = Record<string, unknown>;
export type FieldKind = "string" | "number" | "integer" | "boolean" | "enum" | "array" | "object" | "json";

export interface Field {
  name: string;
  /** Dotted path from the root; the form's error map is keyed by it. */
  path: string;
  kind: FieldKind;
  label: string;
  description?: string;
  required: boolean;
  nullable: boolean;
  schema: Schema;
  enumValues?: unknown[];
  /** Array item: its own kind (a primitive or enum). */
  item?: Field;
  /** Object: its properties. */
  fields?: Field[];
  format?: string;
}

export type Draft = unknown;

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** Follow a local `$ref` (`#/$defs/X` or `#/definitions/X`, as pydantic emits). */
function deref(s: Schema, root: Schema): Schema {
  let cur = s;
  for (let i = 0; i < 8 && typeof cur.$ref === "string"; i++) {
    const ref = cur.$ref as string;
    if (!ref.startsWith("#/")) break;
    let target: unknown = root;
    for (const part of ref.slice(2).split("/")) target = isObj(target) ? target[part.replace(/~1/g, "/").replace(/~0/g, "~")] : undefined;
    if (!isObj(target)) break;
    const { $ref: _ignored, ...rest } = cur;
    cur = { ...target, ...rest };
  }
  return cur;
}

/** Collapse `anyOf: [X, {type: null}]` and `type: [X, "null"]` into X plus a nullable flag. */
export function normalize(schema: Schema, root: Schema = schema): { schema: Schema; nullable: boolean } {
  let s = deref(schema, root);
  let nullable = false;
  for (const key of ["anyOf", "oneOf"] as const) {
    const alts = s[key];
    if (Array.isArray(alts)) {
      const nonNull = alts.filter((a) => !(isObj(a) && a.type === "null"));
      if (nonNull.length !== alts.length) nullable = true;
      if (nonNull.length === 1 && isObj(nonNull[0])) {
        const { [key]: _drop, ...rest } = s;
        s = { ...deref(nonNull[0], root), ...rest };
      }
    }
  }
  if (Array.isArray(s.type)) {
    const types = (s.type as unknown[]).filter((t) => t !== "null");
    if (types.length !== (s.type as unknown[]).length) nullable = true;
    s = { ...s, type: types.length === 1 ? types[0] : types };
  }
  return { schema: s, nullable };
}

function kindOf(s: Schema): FieldKind {
  if (Array.isArray(s.enum) && s.enum.length > 0) return "enum";
  if ("const" in s) return "enum";
  switch (s.type) {
    case "string": return "string";
    case "number": return "number";
    case "integer": return "integer";
    case "boolean": return "boolean";
    case "object": return isObj(s.properties) ? "object" : "json";
    case "array": return "array";
    default:
      if (isObj(s.properties)) return "object";
      return "json";
  }
}

function humanize(name: string): string {
  const t = name.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : name;
}

/** Describe one schema node as a form field. */
export function describe(name: string, path: string, raw: Schema, required: boolean, root: Schema): Field {
  const { schema, nullable } = normalize(raw, root);
  let kind = kindOf(schema);
  const field: Field = {
    name, path, kind, required, nullable, schema,
    label: typeof schema.title === "string" && schema.title ? schema.title : humanize(name),
    description: typeof schema.description === "string" ? schema.description : undefined,
    format: typeof schema.format === "string" ? schema.format : undefined,
  };
  if (kind === "enum") field.enumValues = Array.isArray(schema.enum) ? schema.enum : [schema.const];
  if (kind === "array") {
    const items = isObj(schema.items) ? normalize(schema.items, root).schema : {};
    const itemKind = kindOf(items);
    if (["string", "number", "integer", "boolean", "enum"].includes(itemKind)) {
      field.item = describe(name, `${path}[]`, items, true, root);
    } else {
      kind = "json";
    }
  }
  if (kind === "object") field.fields = objectFields(schema, root, path);
  field.kind = kind;
  return field;
}

/** The fields of an object schema, in declaration order. */
export function objectFields(schema: Schema, root: Schema = schema, prefix = ""): Field[] {
  const s = normalize(schema, root).schema;
  const props = isObj(s.properties) ? s.properties : {};
  const req = new Set(Array.isArray(s.required) ? (s.required as string[]) : []);
  return Object.entries(props).map(([name, sub]) =>
    describe(name, prefix ? `${prefix}.${name}` : name, isObj(sub) ? sub : {}, req.has(name), root));
}

/** True when the schema asks for nothing (no properties): the form is a single Run button. */
export function isEmptySchema(schema: Schema | undefined | null): boolean {
  if (!schema || !isObj(schema)) return true;
  const s = normalize(schema).schema;
  return kindOf(s) !== "object" || objectFields(s).length === 0;
}

// ------------------------------------------------------------------------------------ draft ⇄ value
function initialFor(f: Field): Draft {
  const d = f.schema.default;
  switch (f.kind) {
    case "object": {
      const out: Record<string, Draft> = {};
      for (const c of f.fields ?? []) out[c.name] = isObj(d) && c.name in d ? initialFor({ ...c, schema: { ...c.schema, default: d[c.name] } }) : initialFor(c);
      return out;
    }
    case "boolean": return typeof d === "boolean" ? d : false;
    case "enum": return d !== undefined ? d : f.required && f.enumValues?.length === 1 ? f.enumValues[0] : undefined;
    case "array":
      if (!Array.isArray(d)) return [];
      return f.item?.kind === "enum" || f.item?.kind === "boolean" ? d : d.map((x) => (x === null || x === undefined ? "" : String(x)));
    case "json": return d === undefined ? "" : JSON.stringify(d, null, 2);
    default: return d === undefined || d === null ? "" : String(d);
  }
}

/** The draft a new form starts from: schema defaults, or empty. */
export function initialDraft(schema: Schema): Record<string, Draft> {
  const out: Record<string, Draft> = {};
  for (const f of objectFields(schema)) out[f.name] = initialFor(f);
  return out;
}

const blank = (v: Draft) => v === undefined || v === null || (typeof v === "string" && v.trim() === "");

function num(v: string, integer: boolean): number | null {
  const t = v.trim();
  if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t)) return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return null;
  if (integer && !Number.isInteger(n)) return null;
  return n;
}

/** Problems with one scalar draft (already known to be present). */
function scalarProblem(f: Field, v: Draft): string | null {
  const s = f.schema;
  if (f.kind === "number" || f.kind === "integer") {
    const n = num(String(v), f.kind === "integer");
    if (n === null) return f.kind === "integer" ? "Enter a whole number." : "Enter a number.";
    if (typeof s.minimum === "number" && n < s.minimum) return `Must be at least ${s.minimum}.`;
    if (typeof s.maximum === "number" && n > s.maximum) return `Must be at most ${s.maximum}.`;
    if (typeof s.exclusiveMinimum === "number" && n <= s.exclusiveMinimum) return `Must be greater than ${s.exclusiveMinimum}.`;
    if (typeof s.exclusiveMaximum === "number" && n >= s.exclusiveMaximum) return `Must be less than ${s.exclusiveMaximum}.`;
    if (typeof s.multipleOf === "number" && s.multipleOf > 0 && Math.abs(n / s.multipleOf - Math.round(n / s.multipleOf)) > 1e-9) {
      return `Must be a multiple of ${s.multipleOf}.`;
    }
    return null;
  }
  if (f.kind === "string") {
    const t = String(v);
    if (typeof s.minLength === "number" && t.length < s.minLength) return `Use at least ${s.minLength} characters.`;
    if (typeof s.maxLength === "number" && t.length > s.maxLength) return `Use at most ${s.maxLength} characters.`;
    if (typeof s.pattern === "string") {
      try {
        if (!new RegExp(s.pattern).test(t)) return `Must match the pattern ${s.pattern}.`;
      } catch {
        /* an invalid pattern in a plugin manifest is not the user's problem */
      }
    }
    if (f.format === "email" && !/^[^@\s]+@[^@\s]+$/.test(t)) return "Enter an email address.";
    return null;
  }
  if (f.kind === "enum") {
    return f.enumValues?.some((e) => JSON.stringify(e) === JSON.stringify(v)) ? null : "Choose one of the listed options.";
  }
  if (f.kind === "json") {
    try {
      JSON.parse(String(v));
      return null;
    } catch {
      return "Enter valid JSON.";
    }
  }
  return null;
}

function validateField(f: Field, v: Draft, out: Record<string, string>): void {
  if (f.kind === "object") {
    const obj = isObj(v) ? v : {};
    for (const c of f.fields ?? []) validateField(c, obj[c.name], out);
    return;
  }
  if (f.kind === "boolean") return;
  if (f.kind === "array") {
    const items = (Array.isArray(v) ? v : []).filter((x) => !blank(x));
    const min = typeof f.schema.minItems === "number" ? f.schema.minItems : 0;
    // An optional array left empty is simply not sent; minItems applies once it is used or required.
    if (items.length < min && (f.required || items.length > 0)) {
      out[f.path] = `Add at least ${min} value${min > 1 ? "s" : ""}.`;
      return;
    }
    if (typeof f.schema.maxItems === "number" && items.length > f.schema.maxItems) {
      out[f.path] = `Use at most ${f.schema.maxItems} values.`;
      return;
    }
    if (f.item) {
      for (const [i, x] of items.entries()) {
        const p = scalarProblem(f.item, x);
        if (p) {
          out[f.path] = `Value ${i + 1}: ${p}`;
          return;
        }
      }
    }
    return;
  }
  if (blank(v)) {
    if (f.required && !f.nullable) out[f.path] = "This field is required.";
    return;
  }
  const p = scalarProblem(f, v);
  if (p) out[f.path] = p;
}

/** Every problem in the draft, keyed by field path. Empty means the value can be submitted. */
export function validate(schema: Schema, draft: Record<string, Draft>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of objectFields(schema)) validateField(f, draft[f.name], out);
  return out;
}

function scalarValue(f: Field, v: Draft): unknown {
  switch (f.kind) {
    case "number": case "integer": return num(String(v), f.kind === "integer");
    case "json": return JSON.parse(String(v));
    case "string": return String(v);
    default: return v;
  }
}

function valueOf(f: Field, v: Draft): { set: boolean; value?: unknown } {
  if (f.kind === "object") {
    const obj = isObj(v) ? v : {};
    const out: Record<string, unknown> = {};
    for (const c of f.fields ?? []) {
      const r = valueOf(c, obj[c.name]);
      if (r.set) out[c.name] = r.value;
    }
    return Object.keys(out).length || f.required ? { set: true, value: out } : { set: false };
  }
  if (f.kind === "boolean") return { set: true, value: Boolean(v) };
  if (f.kind === "array") {
    const items = (Array.isArray(v) ? v : []).filter((x) => !blank(x));
    if (!items.length && !f.required) return { set: false };
    return { set: true, value: f.item ? items.map((x) => scalarValue(f.item!, x)) : items };
  }
  if (blank(v)) return f.nullable && f.required ? { set: true, value: null } : { set: false };
  return { set: true, value: scalarValue(f, v) };
}

/** The submitted value: typed, with empty optional fields left out. Call after `validate` is clean. */
export function toValue(schema: Schema, draft: Record<string, Draft>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of objectFields(schema)) {
    const r = valueOf(f, draft[f.name]);
    if (r.set) out[f.name] = r.value;
  }
  return out;
}

/** Set a value at a dotted path in a draft, returning a new draft (immutable update). */
export function setAt(draft: Record<string, Draft>, path: string, value: Draft): Record<string, Draft> {
  const [head, ...rest] = path.split(".");
  if (!rest.length) return { ...draft, [head]: value };
  const child = isObj(draft[head]) ? (draft[head] as Record<string, Draft>) : {};
  return { ...draft, [head]: setAt(child, rest.join("."), value) };
}

export function getAt(draft: Record<string, Draft>, path: string): Draft {
  let cur: unknown = draft;
  for (const p of path.split(".")) cur = isObj(cur) ? cur[p] : undefined;
  return cur;
}
