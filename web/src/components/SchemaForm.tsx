import { useId, useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  getAt, initialDraft, objectFields, setAt, toValue, validate, type Draft, type Field, type Schema,
} from "../lib/jsonSchema";

/**
 * A form generated from a capability's `input_schema` (P4-U07). Built in-house rather than with
 * @rjsf/core: the schemas here are small and flat, rjsf plus its AJV validator would roughly
 * double the bundle and compile validators with `new Function` (a CSP problem), and its default
 * widgets do not use our tokens or the label/description/error wiring axe checks.
 */
export function SchemaForm({ schema, onSubmit, submitLabel = "Run", busy = false, disabled = false, footer }: {
  schema: Schema;
  onSubmit: (value: Record<string, unknown>) => void;
  submitLabel?: string;
  busy?: boolean;
  disabled?: boolean;
  footer?: ReactNode;
}) {
  const fields = useMemo(() => objectFields(schema), [schema]);
  const [draft, setDraft] = useState<Record<string, Draft>>(() => initialDraft(schema));
  const [submitted, setSubmitted] = useState(false);
  const errors = useMemo(() => validate(schema, draft), [schema, draft]);
  const shown = submitted ? errors : {};
  const idBase = useId();
  const count = Object.keys(errors).length;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (count) return;
    onSubmit(toValue(schema, draft));
  };
  const change = (path: string, v: Draft) => setDraft((d) => setAt(d, path, v));

  return (
    <form className="form schema-form" onSubmit={submit} noValidate>
      {fields.length === 0 && <p className="muted small">This capability takes no inputs.</p>}
      {fields.map((f) => (
        <FieldView key={f.path} field={f} idBase={idBase} draft={draft} errors={shown} onChange={change} disabled={disabled || busy} />
      ))}
      {submitted && count > 0 && (
        <p className="field-error" role="alert">{count === 1 ? "1 field needs attention." : `${count} fields need attention.`}</p>
      )}
      {footer}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={disabled || busy}>{busy ? "Running…" : submitLabel}</button>
      </div>
    </form>
  );
}

function fieldId(idBase: string, path: string) {
  return `${idBase}-${path.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

function Label({ field, htmlFor }: { field: Field; htmlFor?: string }) {
  return (
    <label htmlFor={htmlFor}>
      {field.label}
      {field.required && !field.nullable ? <span className="req" aria-hidden="true"> *</span> : <span className="muted small"> (optional)</span>}
    </label>
  );
}

function FieldView({ field: f, idBase, draft, errors, onChange, disabled }: {
  field: Field; idBase: string; draft: Record<string, Draft>; errors: Record<string, string>;
  onChange: (path: string, v: Draft) => void; disabled: boolean;
}) {
  const id = fieldId(idBase, f.path);
  const descId = f.description ? `${id}-desc` : undefined;
  const err = errors[f.path];
  const errId = err ? `${id}-err` : undefined;
  const describedBy = [descId, errId].filter(Boolean).join(" ") || undefined;
  const value = getAt(draft, f.path);
  const common = { id, disabled, "aria-describedby": describedBy, "aria-invalid": err ? true : undefined,
    "aria-required": f.required && !f.nullable ? true : undefined } as const;

  const hint = f.description ? <div className="field-hint" id={descId}>{f.description}</div> : null;
  const error = err ? <div className="field-error" id={errId}>{err}</div> : null;

  if (f.kind === "object") {
    return (
      <fieldset className="schema-fieldset" aria-describedby={descId}>
        <legend>{f.label}</legend>
        {hint}
        {(f.fields ?? []).map((c) => <FieldView key={c.path} field={c} idBase={idBase} draft={draft} errors={errors} onChange={onChange} disabled={disabled} />)}
      </fieldset>
    );
  }

  if (f.kind === "boolean") {
    return (
      <div className="field field-check">
        <label htmlFor={id} className="check">
          <input type="checkbox" {...common} checked={Boolean(value)} onChange={(e) => onChange(f.path, e.target.checked)} /> {f.label}
        </label>
        {hint}
      </div>
    );
  }

  if (f.kind === "enum") {
    const opts = f.enumValues ?? [];
    const idx = opts.findIndex((o) => JSON.stringify(o) === JSON.stringify(value));
    return (
      <div className="field">
        <Label field={f} htmlFor={id} />
        <select {...common} value={idx >= 0 ? String(idx) : ""} onChange={(e) => onChange(f.path, e.target.value === "" ? undefined : opts[Number(e.target.value)])}>
          <option value="">{f.required && !f.nullable ? "Choose…" : "— not set —"}</option>
          {opts.map((o, i) => <option key={i} value={String(i)}>{typeof o === "string" ? o : JSON.stringify(o)}</option>)}
        </select>
        {hint}{error}
      </div>
    );
  }

  if (f.kind === "array" && f.item) {
    const items = Array.isArray(value) ? value : [];
    if (f.item.kind === "enum") {
      const opts = f.item.enumValues ?? [];
      const has = (o: unknown) => items.some((x) => JSON.stringify(x) === JSON.stringify(o));
      return (
        <fieldset className="schema-fieldset" aria-describedby={describedBy} aria-invalid={err ? true : undefined}>
          <legend>{f.label}{f.required ? "" : <span className="muted small"> (optional)</span>}</legend>
          {hint}
          <div className="chip-row">
            {opts.map((o, i) => (
              <label key={i} className="check" htmlFor={`${id}-${i}`}>
                <input id={`${id}-${i}`} type="checkbox" disabled={disabled} checked={has(o)}
                  onChange={(e) => onChange(f.path, e.target.checked ? [...items, o] : items.filter((x) => JSON.stringify(x) !== JSON.stringify(o)))} />
                {" "}{typeof o === "string" ? o : JSON.stringify(o)}
              </label>
            ))}
          </div>
          {error}
        </fieldset>
      );
    }
    return (
      <fieldset className="schema-fieldset" aria-describedby={describedBy}>
        <legend>{f.label}{f.required ? "" : <span className="muted small"> (optional)</span>}</legend>
        {hint}
        {items.length === 0 && <p className="muted small">No values.</p>}
        {items.map((x, i) => (
          <div key={i} className="schema-array-row">
            <label className="sr-only" htmlFor={`${id}-${i}`}>{`${f.label} ${i + 1}`}</label>
            {f.item!.kind === "boolean" ? (
              <input id={`${id}-${i}`} type="checkbox" disabled={disabled} checked={Boolean(x)}
                onChange={(e) => onChange(f.path, items.map((y, j) => (j === i ? e.target.checked : y)))} />
            ) : (
              <input id={`${id}-${i}`} type="text" inputMode={f.item!.kind === "string" ? undefined : "decimal"} disabled={disabled}
                value={String(x ?? "")} aria-invalid={err ? true : undefined}
                onChange={(e) => onChange(f.path, items.map((y, j) => (j === i ? e.target.value : y)))} />
            )}
            <button type="button" className="btn btn-xs btn-ghost" disabled={disabled} aria-label={`Remove ${f.label} ${i + 1}`}
              onClick={() => onChange(f.path, items.filter((_, j) => j !== i))}>Remove</button>
          </div>
        ))}
        <div>
          <button type="button" className="btn btn-xs" disabled={disabled}
            onClick={() => onChange(f.path, [...items, f.item!.kind === "boolean" ? false : ""])}>Add {f.label.toLowerCase()}</button>
        </div>
        {error}
      </fieldset>
    );
  }

  if (f.kind === "json" || f.kind === "array") {
    return (
      <div className="field">
        <Label field={f} htmlFor={id} />
        <textarea {...common} className="mono" rows={4} spellCheck={false} value={String(value ?? "")}
          onChange={(e) => onChange(f.path, e.target.value)} />
        <div className="field-hint">Enter JSON.</div>
        {hint}{error}
      </div>
    );
  }

  const numeric = f.kind === "number" || f.kind === "integer";
  const long = f.kind === "string" && (typeof f.schema.maxLength !== "number" || f.schema.maxLength > 200) && f.format === "textarea";
  return (
    <div className="field">
      <Label field={f} htmlFor={id} />
      {long ? (
        <textarea {...common} rows={3} value={String(value ?? "")} onChange={(e) => onChange(f.path, e.target.value)} />
      ) : (
        <input {...common} type={f.format === "date" ? "date" : f.format === "email" ? "email" : "text"}
          inputMode={numeric ? (f.kind === "integer" ? "numeric" : "decimal") : undefined}
          placeholder={f.schema.examples && Array.isArray(f.schema.examples) ? String(f.schema.examples[0]) : undefined}
          value={String(value ?? "")} onChange={(e) => onChange(f.path, e.target.value)} />
      )}
      {hint}{error}
    </div>
  );
}
