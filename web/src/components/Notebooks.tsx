import { useId, useState, type FormEvent } from "react";
import { api, type CellType, type NotebookCell, type StepRevision } from "../api";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast } from "../routes";
import { StepResultView, StepVersions } from "./DataThread";
import { Markdown } from "./Markdown";
import { isStaleEdit, StaleEditNotice } from "./StaleEdit";
import { VerificationBadge } from "./WhyNumber";
import { CodeBlock, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge, Tag } from "./ui";

const CELL_WORDS: Record<string, string> = { markdown: "Markdown", sql: "SQL", python: "Python" };
const CELL_HINTS: Record<string, string> = {
  markdown: "Notes; numbers you write here are not checked.",
  sql: "Runs through the query gateway on your scope, like every other query.",
  python: "Restricted Python on earlier cells' results: inputs[\"cell<N>\"] as records; set result. Imports: math, statistics, json, numpy, pandas.",
};

function CellCard({ wsId, notebookId, cell, index, canEdit, onRevised }: {
  wsId: string; notebookId: string; cell: NotebookCell; index: number; canEdit: boolean; onRevised: (r: StepRevision) => void;
}) {
  const id = useId();
  const detail = useAsync(() => (cell.cell === "markdown" ? Promise.resolve(undefined) : api.step(wsId, cell.id)), [wsId, cell.id, cell.version, cell.cell]);
  const [editing, setEditing] = useState(false);
  const [versions, setVersions] = useState(false);
  const [source, setSource] = useState(cell.source);
  const [mine, setMine] = useState<unknown>(null);
  const act = useAction();
  const name = `Cell ${index + 1} (${CELL_WORDS[cell.cell] ?? cell.cell})`;
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setMine({ source, version: cell.current_version });
    const r = await act.run(() => api.editCell(wsId, notebookId, cell.id, cell.current_version, { source }));
    if (r) {
      setEditing(false);
      onRevised(r);
    }
  };
  return (
    <li className="step-card" data-status={cell.status} data-void={cell.verification_record?.badge === "void" ? "true" : undefined} aria-label={name}>
      <div className="step-head">
        <h3>{cell.title && cell.title !== "Cell" ? cell.title : name}</h3>
        <Tag>{CELL_WORDS[cell.cell] ?? cell.cell}</Tag>
        <span className="small muted">v{cell.version}</span>
        <StatusBadge status={cell.status} />
      </div>
      {cell.cell !== "markdown" && <div className="small"><VerificationBadge state={cell.verification_record} /></div>}
      {cell.status === "failed" && (
        <div className="alert alert-danger" role="alert">
          <strong>Version {cell.version} failed:</strong> {cell.error ?? "the cell did not run"}.
          {cell.version > 1 ? " Earlier versions and their results stay under Versions; cells that read this one are not re-run on a failed result." : " Edit the cell to try again."}
        </div>
      )}
      {cell.cell === "markdown" ? <Markdown text={cell.source} /> : (
        <details className="small"><summary>{CELL_WORDS[cell.cell]} source</summary><CodeBlock code={cell.source} /></details>
      )}
      <ErrorBox error={detail.error} />
      {detail.data && cell.status !== "failed" && <StepResultView wsId={wsId} step={detail.data} result={detail.data.result} />}
      <div className="step-actions">
        {canEdit && <button type="button" className="btn btn-xs" aria-expanded={editing} onClick={() => { setSource(cell.source); setEditing((v) => !v); }}>
          Edit<span className="sr-only"> {name}</span></button>}
        <button type="button" className="btn btn-xs btn-ghost" aria-expanded={versions} onClick={() => setVersions((v) => !v)}>
          Versions ({cell.current_version})<span className="sr-only"> of {name}</span></button>
      </div>
      {editing && (
        <form className="form step-edit" onSubmit={save} aria-label={`Edit ${name}`}>
          <Field label="Source" htmlFor={`${id}-src`} hint={CELL_HINTS[cell.cell]}>
            <textarea id={`${id}-src`} rows={Math.min(12, Math.max(3, source.split("\n").length + 1))} value={source} onChange={(e) => setSource(e.target.value)} />
          </Field>
          {isStaleEdit(act.failure)
            ? <StaleEditNotice what="This cell" mine={mine} loadCurrent={async () => { const c = await api.step(wsId, cell.id); return { source: c.origin.source, version: c.current_version }; }}
              onReload={() => { act.clear(); setEditing(false); onRevised({ step: cell, rerun: [], voided_records: [] }); }} />
            : <ErrorBox error={act.error} />}
          <div className="form-actions">
            <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Running…" : "Save and run"}</button>
          </div>
        </form>
      )}
      {versions && <StepVersions wsId={wsId} stepId={cell.id} />}
    </li>
  );
}

function AddCellForm({ wsId, notebookId, onAdded }: { wsId: string; notebookId: string; onAdded: () => void }) {
  const id = useId();
  const [cell, setCell] = useState<CellType>("sql");
  const [source, setSource] = useState("");
  const [title, setTitle] = useState("");
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!source.trim()) return;
    const r = await act.run(() => api.addCell(wsId, notebookId, { cell, source, title: title.trim() || null }));
    if (r) {
      setSource("");
      setTitle("");
      onAdded();
    }
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Add a cell">
      <div className="form-row">
        <Field label="Cell type" htmlFor={`${id}-t`} hint={CELL_HINTS[cell]}>
          <select id={`${id}-t`} value={cell} onChange={(e) => setCell(e.target.value as CellType)}>
            <option value="markdown">Markdown</option><option value="sql">SQL</option><option value="python">Python (restricted)</option>
          </select>
        </Field>
        <Field label="Title" htmlFor={`${id}-ti`}><input id={`${id}-ti`} value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
      </div>
      <Field label="Source" htmlFor={`${id}-s`}>
        <textarea id={`${id}-s`} rows={4} value={source} onChange={(e) => setSource(e.target.value)} className="step-edit" />
      </Field>
      <ErrorBox error={act.error} />
      <div className="form-actions"><button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || !source.trim()}>{act.busy ? "Running…" : "Add and run"}</button></div>
    </form>
  );
}

function NotebookView({ wsId, id, canEdit }: { wsId: string; id: string; canEdit: boolean }) {
  const nb = useAsync(() => api.notebook(wsId, id), [wsId, id]);
  const [notice, setNotice] = useState<string | null>(null);
  const act = useAction();
  const onRevised = (r: StepRevision) => {
    if (r.rerun.length || r.voided_records.length || r.step.version > 1) {
      setNotice(`Cell re-ran as version ${r.step.version}${r.step.status === "failed" ? " and failed" : ""}. `
        + `${r.rerun.length ? `${r.rerun.length} cell${r.rerun.length > 1 ? "s" : ""} that read it re-ran` : "No later cell reads it"}`
        + `${r.voided_records.length ? `; ${r.voided_records.length} earlier verdict${r.voided_records.length > 1 ? "s are" : " is"} now void` : ""}.`);
    }
    void nb.reload();
  };
  const runAll = async () => {
    const r = await act.run(() => api.runNotebook(wsId, id));
    if (r) {
      nb.setData(r);
      setNotice(`Ran ${r.executed?.length ?? r.cells.length} cell${(r.executed?.length ?? r.cells.length) === 1 ? "" : "s"} in order on today's data.`);
    }
  };
  if (nb.error) return <ErrorBox error={nb.error} onRetry={nb.reload} />;
  if (!nb.data) return <Loading />;
  const n = nb.data;
  return (
    <div className="stack" aria-label={`Notebook ${n.title}`} role="region">
      <div className="toolbar">
        <h2 className="h-sm">{n.title} <span className="muted small">revision {n.revision}</span></h2>
        {canEdit && n.cells.length > 0 && <button type="button" className="btn btn-sm" onClick={() => void runAll()} disabled={act.busy}>
          {act.busy ? "Running…" : "Run all cells"}</button>}
      </div>
      <p className="muted small">Each cell is a step: SQL goes through the query gateway, Python runs restricted in the sandbox on earlier results, and
        every edit is a new version.</p>
      {notice && <Notice tone="info">{notice}</Notice>}
      <ErrorBox error={act.error} />
      {n.cells.length === 0 && <EmptyState title="No cells yet">{canEdit ? "Add a Markdown, SQL or Python cell below." : "An analyst adds cells."}</EmptyState>}
      {n.cells.length > 0 && (
        <ol className="cell-list" aria-label="Cells">
          {n.cells.map((c, i) => <CellCard key={`${c.id}-${c.version}`} wsId={wsId} notebookId={id} cell={c} index={i} canEdit={canEdit} onRevised={onRevised} />)}
        </ol>
      )}
      {canEdit && (
        <details className="card" open={n.cells.length === 0}>
          <summary>Add a cell</summary>
          <div className="card-body"><AddCellForm wsId={wsId} notebookId={id} onAdded={() => void nb.reload()} /></div>
        </details>
      )}
    </div>
  );
}

/** Work → Notebooks (P7-12): markdown, SQL and restricted-Python cells, each a versioned step. */
export function NotebooksPanel({ wsId, role, selected, onSelect }: { wsId: string; role: string | undefined; selected: string | null; onSelect: (id: string | null) => void }) {
  const list = useAsync(() => api.notebooks(wsId), [wsId]);
  const id = useId();
  const [title, setTitle] = useState("");
  const act = useAction();
  const canEdit = roleAtLeast(role, "analyst");
  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    const r = await act.run(() => api.createNotebook(wsId, title.trim()));
    if (r) {
      setTitle("");
      await list.reload();
      onSelect(r.id);
    }
  };
  return (
    <div className="split">
      <div className="split-list stack">
        {canEdit && (
          <form className="form" onSubmit={create} aria-label="New notebook">
            <Field label="New notebook" htmlFor={`${id}-t`}><input id={`${id}-t`} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title" /></Field>
            <ErrorBox error={act.error} />
            <button type="submit" className="btn btn-sm btn-primary" disabled={act.busy || !title.trim()}>Create notebook</button>
          </form>
        )}
        <ErrorBox error={list.error} onRetry={list.reload} />
        {list.loading && !list.data && <Loading />}
        {list.data?.length === 0 && <EmptyState title="No notebooks yet" />}
        {!!list.data?.length && (
          <ul className="list selectable" aria-label="Notebooks">
            {list.data.map((n) => (
              <li key={n.id}>
                <button type="button" className={`list-button ${n.id === selected ? "active" : ""}`} aria-current={n.id === selected ? "true" : undefined}
                  onClick={() => onSelect(n.id)}>
                  <span>{n.title}</span>
                  <span className="small muted">{typeof n.cells === "number" ? `${n.cells} cells` : ""}{n.updated_at ? ` · ${fmtDate(n.updated_at)}` : ""}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="split-detail">
        {selected ? <NotebookView key={selected} wsId={wsId} id={selected} canEdit={canEdit} />
          : <EmptyState title="Select a notebook">Notebooks keep exploratory SQL and Python as governed, versioned steps.</EmptyState>}
      </div>
    </div>
  );
}
