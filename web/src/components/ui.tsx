import { useId, useState, type ReactNode } from "react";
import { fmtValue } from "../lib/format";
import { toneFor } from "../lib/status";

export function StatusBadge({ status, label }: { status: string | null | undefined; label?: string }) {
  const tone = toneFor(status);
  return (
    <span className={`badge badge-${tone}`} data-tone={tone}>
      <span className="badge-dot" aria-hidden="true" />
      {label ?? status ?? "unknown"}
    </span>
  );
}

export function Tag({ children, tone = "neutral" }: { children: ReactNode; tone?: string }) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

export function ErrorBox({ error, onRetry }: { error: string | null | undefined; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div className="alert alert-danger" role="alert">
      <strong>Error:</strong> <span>{error}</span>
      {onRetry && (
        <button type="button" className="btn btn-sm btn-ghost" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Notice({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "warning" | "success" | "danger" }) {
  return (
    <div className={`alert alert-${tone}`} role={tone === "danger" ? "alert" : "status"}>
      {children}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <p className="empty-title">{title}</p>
      {children && <div className="empty-body">{children}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" /> {label}
    </div>
  );
}

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-header">
          {title && <h2 className="card-title">{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

export function ConfidenceBar({ value }: { value: number | null | undefined }) {
  const v = Math.max(0, Math.min(1, Number(value ?? 0)));
  const tone = v >= 0.7 ? "success" : v >= 0.45 ? "warning" : "danger";
  return (
    <div className="confidence" title={`confidence ${(v * 100).toFixed(0)}%`}>
      <div className="confidence-track" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v * 100)}
        aria-label="Confidence">
        <div className={`confidence-fill fill-${tone}`} style={{ width: `${v * 100}%` }} />
      </div>
      <span className="confidence-label">{(v * 100).toFixed(0)}%</span>
    </div>
  );
}

export function DataTable({ columns, rows, maxRows = 200, caption }: { columns: string[]; rows: unknown[][]; maxRows?: number; caption?: string }) {
  if (!columns.length) return <EmptyState title="No columns" />;
  const shown = rows.slice(0, maxRows);
  return (
    <div className="table-wrap">
      <table className="table table-compact">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c} scope="col">{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i}>
              {columns.map((_, j) => (
                <td key={j} className={typeof r[j] === "number" ? "num" : undefined}>{fmtValue(r[j])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && <p className="muted small">Showing {shown.length} of {rows.length} rows.</p>}
      {rows.length === 0 && <p className="muted small">No rows.</p>}
    </div>
  );
}

/** Render records (list of objects) as a table. */
export function RecordTable({ records, maxRows = 50 }: { records: Record<string, unknown>[]; maxRows?: number }) {
  if (!records.length) return <p className="muted small">No rows.</p>;
  const cols = [...new Set(records.flatMap((r) => Object.keys(r)))];
  return <DataTable columns={cols} rows={records.map((r) => cols.map((c) => r[c]))} maxRows={maxRows} />;
}

/** Normalise a result preview that may be list-of-lists or list-of-objects. */
export function PreviewTable({ columns, preview, maxRows = 50 }: { columns: string[]; preview: unknown[]; maxRows?: number }) {
  if (!preview?.length) return <p className="muted small">No preview rows recorded.</p>;
  if (Array.isArray(preview[0])) return <DataTable columns={columns} rows={preview as unknown[][]} maxRows={maxRows} />;
  return <RecordTable records={preview as Record<string, unknown>[]} maxRows={maxRows} />;
}

export function CodeBlock({ code, label }: { code: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="code">
      <div className="code-bar">
        <span className="muted small">{label ?? "SQL"}</span>
        <button type="button" className="btn btn-xs btn-ghost" onClick={() => {
          void navigator.clipboard?.writeText(code).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1200);
          });
        }}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  );
}

export function JsonView({ value, collapsed = false, label = "JSON" }: { value: unknown; collapsed?: boolean; label?: string }) {
  const text = JSON.stringify(value, null, 2);
  if (collapsed) {
    return (
      <details className="json">
        <summary>{label}</summary>
        <pre>{text}</pre>
      </details>
    );
  }
  return <pre className="json">{text}</pre>;
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v]) => (
        <div key={k} className="kv-row">
          <dt>{k}</dt>
          <dd>{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: ReactNode }[]; value: T; onChange: (t: T) => void }) {
  const id = useId();
  return (
    <div className="tabs" role="tablist" aria-label="Sections">
      {tabs.map((t) => (
        <button key={t.id} id={`${id}-${t.id}`} role="tab" type="button" aria-selected={value === t.id}
          className={`tab ${value === t.id ? "tab-active" : ""}`} onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="field">
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  );
}
