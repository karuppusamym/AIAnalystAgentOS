import { useId, useState, type FormEvent } from "react";
import { api, type QueryResult } from "../api";
import { fmtValue, shortHash } from "../lib/format";
import { useAction } from "../lib/hooks";
import { DataTable, ErrorBox } from "./ui";

export type FilterOp = "=" | "!=" | ">" | ">=" | "<" | "<=" | "contains";
export interface PreviewFilter { column: string; op: FilterOp; value: string }

const OPS: { id: FilterOp; label: string }[] = [
  { id: "=", label: "is" }, { id: "!=", label: "is not" }, { id: ">", label: ">" }, { id: ">=", label: "≥" }, { id: "<", label: "<" },
  { id: "<=", label: "≤" }, { id: "contains", label: "contains" },
];

const ident = (c: string) => `"${c.replace(/"/g, '""')}"`;
const literal = (v: string, numeric: boolean) => (numeric && v.trim() !== "" && Number.isFinite(Number(v)) ? String(Number(v)) : `'${v.replace(/'/g, "''")}'`);

/**
 * The governed re-query for a preview filter: the original statement as a subquery with the filters
 * as a WHERE clause. It is sent to the query gateway like any other SQL (validated, scoped, audited),
 * so a filter changes the numbers only by re-reading the data, never by hiding rows in the browser.
 */
export function filteredSql(sql: string, filters: PreviewFilter[], numericColumns: Set<string>): string {
  const base = sql.trim().replace(/;+\s*$/, "");
  if (!filters.length) return base;
  const where = filters.map((f) => (f.op === "contains"
    ? `CAST(${ident(f.column)} AS TEXT) LIKE ${literal(`%${f.value}%`, false)}`
    : `${ident(f.column)} ${f.op} ${literal(f.value, numericColumns.has(f.column))}`)).join(" AND ");
  return `SELECT * FROM (\n${base}\n) AS filtered\nWHERE ${where}`;
}

/**
 * A result table with working filters (workbench-ux §4, P4-07): applying a filter re-queries through
 * the gateway and shows the applied filters and the new evidence (row count, result hash). Until a
 * filter is applied the original result is shown unchanged; a refused re-query leaves it unchanged
 * and says so.
 */
export function FilteredPreview({ wsId, sql, columns, rows, caption }: { wsId: string; sql: string; columns: string[]; rows: unknown[][]; caption: string }) {
  const id = useId();
  const numeric = new Set(columns.filter((_, i) => rows.length > 0 && rows.every((r) => r[i] === null || typeof r[i] === "number")));
  const [column, setColumn] = useState(columns[0] ?? "");
  const [op, setOp] = useState<FilterOp>("=");
  const [value, setValue] = useState("");
  const [applied, setApplied] = useState<PreviewFilter[]>([]);
  const [result, setResult] = useState<QueryResult | null>(null);
  const act = useAction();
  const run = async (filters: PreviewFilter[]) => {
    if (!filters.length) {
      setApplied([]);
      setResult(null);
      return;
    }
    const r = await act.run(() => api.query(wsId, filteredSql(sql, filters, numeric)));
    if (r) {
      setApplied(filters);
      setResult(r);
    }
  };
  const add = (e: FormEvent) => {
    e.preventDefault();
    if (!column || value.trim() === "") return;
    void run([...applied, { column, op, value: value.trim() }]);
    setValue("");
  };
  const shown = result ?? { columns, rows, row_count: rows.length } as Pick<QueryResult, "columns" | "rows" | "row_count">;
  return (
    <div className="stack filtered-preview">
      <form className="filter-bar" onSubmit={add} aria-label={`Filter ${caption}`}>
        <label className="inline-field small" htmlFor={`${id}-c`}>Column
          <select id={`${id}-c`} value={column} onChange={(e) => setColumn(e.target.value)}>
            {columns.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="inline-field small" htmlFor={`${id}-o`}>Condition
          <select id={`${id}-o`} value={op} onChange={(e) => setOp(e.target.value as FilterOp)}>
            {OPS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
        </label>
        <label className="inline-field small" htmlFor={`${id}-v`}>Value
          <input id={`${id}-v`} value={value} onChange={(e) => setValue(e.target.value)} size={10} />
        </label>
        <button type="submit" className="btn btn-xs" disabled={act.busy || value.trim() === ""}>{act.busy ? "Re-querying…" : "Apply filter"}</button>
      </form>
      {applied.length > 0 && result && (
        <div className="chip-row small" role="status" aria-label="Applied filters">
          <span>Re-queried through the gateway with</span>
          {applied.map((f, k) => (
            <span key={k} className="chip">{f.column} {OPS.find((o) => o.id === f.op)?.label} {fmtValue(f.value)}
              <button type="button" className="btn btn-xs btn-ghost" aria-label={`Remove filter ${f.column} ${f.op} ${f.value}`}
                onClick={() => void run(applied.filter((_, i) => i !== k))}>×</button></span>
          ))}
          <span className="muted">· {result.row_count} row{result.row_count === 1 ? "" : "s"}{result.result_hash ? ` · result ${shortHash(result.result_hash, 10)}` : ""}
            {result.query_id ? ` · query ${result.query_id}` : ""}</span>
          <button type="button" className="btn btn-xs btn-ghost" onClick={() => void run([])}>Clear filters</button>
        </div>
      )}
      {act.error && <ErrorBox error={`The filter was not applied (the numbers below are unchanged): ${act.error}`} />}
      <DataTable columns={shown.columns} rows={shown.rows} caption={applied.length ? `${caption}, filtered` : caption} maxRows={20} />
    </div>
  );
}
