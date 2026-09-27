import { useId, useState, type FormEvent } from "react";
import { api, type BriefPatchBody, type CatalogColumn } from "../api";
import { useAction } from "../lib/hooks";
import { isStaleEdit, StaleEditNotice } from "./StaleEdit";
import { ErrorBox, Field } from "./ui";

/** The brief operations a column curation makes: a person's unit and glossary alias for `<asset>.<column>`. */
export function curationOps(fqColumn: string, unit: string, alias: string, note: string): BriefPatchBody["ops"] {
  const ops: BriefPatchBody["ops"] = [];
  if (unit.trim()) ops.push({ op: "set", assertion: { group: "time_measures", field: "unit", subject: fqColumn, value: unit.trim(), note: note.trim() || null, evidence: [] } });
  if (alias.trim()) ops.push({ op: "set", assertion: { group: "domain", field: "alias", subject: fqColumn, value: alias.trim(), note: note.trim() || null, evidence: [] } });
  return ops;
}

/**
 * Curate one column (P4-07): its tags (a person's tags are never removed by the crawler, and crawler
 * tags only tighten), and its unit and glossary alias as facts in the workspace brief, made against
 * the brief version on screen so a concurrent edit is reported instead of overwritten.
 */
export function ColumnCurationForm({ wsId, assetId, fq, column, onDone, onCancel }: {
  wsId: string; assetId: string; fq: string; column: CatalogColumn; onDone: (message: string) => void; onCancel: () => void;
}) {
  const id = useId();
  const [tags, setTags] = useState(column.tags.join(", "));
  const [unit, setUnit] = useState(column.unit ?? "");
  const [alias, setAlias] = useState(column.glossary?.term ?? "");
  const [note, setNote] = useState("");
  const [mine, setMine] = useState<unknown>(null);
  const act = useAction();
  const target = `${fq}.${column.name}`;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const nextTags = tags.split(",").map((t) => t.trim().toLowerCase()).filter(Boolean);
    const ops = curationOps(target, unit !== (column.unit ?? "") ? unit : "", alias !== (column.glossary?.term ?? "") ? alias : "", note);
    const r = await act.run(async () => {
      const changed: string[] = [];
      if (nextTags.join(",") !== column.tags.join(",")) {
        await api.tagColumn(assetId, column.name, nextTags);
        changed.push("tags");
      }
      if (ops.length) {
        const brief = await api.brief(wsId);
        setMine({ version: brief.version, ops });
        const out = await api.patchBrief(wsId, brief.version, { ops, reason: `curated ${target}` });
        changed.push(`brief v${out.version}`);
      }
      return changed;
    });
    if (r) onDone(r.length ? `Saved ${target}: ${r.join(", ")}.` : "Nothing changed.");
  };
  return (
    <form className="form" onSubmit={submit} aria-label={`Curate ${column.name}`}>
      <div className="form-row">
        <Field label="Tags" htmlFor={`${id}-tags`} hint="Comma separated, e.g. pii, restricted. Your tags are never removed by a crawl.">
          <input id={`${id}-tags`} value={tags} onChange={(e) => setTags(e.target.value)} />
        </Field>
        <Field label="Unit" htmlFor={`${id}-unit`}><input id={`${id}-unit`} value={unit} onChange={(e) => setUnit(e.target.value)} placeholder="hours" /></Field>
        <Field label="Glossary alias" htmlFor={`${id}-alias`}><input id={`${id}-alias`} value={alias} onChange={(e) => setAlias(e.target.value)} placeholder="time to resolve" /></Field>
      </div>
      <Field label="Note" htmlFor={`${id}-note`}><input id={`${id}-note`} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      <p className="small muted">Unit and alias become facts in the workspace brief (stated by you), used by readiness and planning.</p>
      {isStaleEdit(act.failure)
        ? <StaleEditNotice what="The workspace brief" mine={mine} loadCurrent={() => api.brief(wsId)} onReload={() => act.clear()} />
        : <ErrorBox error={act.error} />}
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy}>{act.busy ? "Saving…" : "Save curation"}</button>
      </div>
    </form>
  );
}
