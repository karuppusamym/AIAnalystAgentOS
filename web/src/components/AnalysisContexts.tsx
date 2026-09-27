import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type DefinitionVersion } from "../api";
import { analysisContexts, contextSpec, type AnalysisContextSpec } from "../lib/analysisContexts";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge } from "./ui";

const blank: AnalysisContextSpec = { purpose: "", business_description: "", question_template: "", source_ids: [], metric_names: [] };
const slug = (name: string) => name.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 100);

/** One business purpose over reusable sources and metrics. Each published version is immutable. */
export function AnalysisContexts({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const contexts = useAsync(() => analysisContexts(wsId), [wsId]);
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const catalog = useAsync(() => api.catalog(wsId), [wsId]);
  const act = useAction();
  const [selected, setSelected] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [spec, setSpec] = useState<AnalysisContextSpec>(blank);
  const [note, setNote] = useState("");
  const current = contexts.data?.find((c) => c.id === selected);
  const editable = !current || current.status === "draft" || current.status === "tested";
  const dirty = !!current && (title !== (current.title ?? "") || JSON.stringify(spec) !== JSON.stringify(contextSpec(current)));

  const choose = (c: DefinitionVersion | null) => {
    setSelected(c?.id ?? null);
    setKey(c?.key ?? "");
    setTitle(c?.title ?? "");
    setSpec(c ? contextSpec(c) : { ...blank, source_ids: [] });
    setNote("");
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const definitionKey = current?.key ?? (key || slug(title));
    if (!definitionKey || spec.source_ids.length === 0) return;
    const body = { ...spec, metric_names: spec.metric_names.filter(Boolean) };
    const saved = await act.run(() => current && editable
      ? api.updateDefinition(wsId, current.id, current.revision, { title: title.trim(), spec: body })
      : api.createDefinition(wsId, { kind: "analysis_context", key: definitionKey, title: title.trim(), spec: body }));
    if (saved) { setSelected(saved.id); setNote("Draft saved. Review its purpose, question and sources before publishing."); void contexts.reload(); }
  };
  const publish = async () => {
    if (!current || !editable) return;
    const published = await act.run(() => api.publishDefinition(wsId, current.id, current.revision));
    if (published) { setNote(`Version ${published.version} published. Investigations can now use it.`); void contexts.reload(); }
  };
  const latest = (contexts.data ?? []).filter((c) => !["deprecated", "retired"].includes(c.status));
  const catalogNotes = (catalog.data ?? []).filter((a) => spec.source_ids.includes(a.source_id) && a.selected && a.description)
    .slice(0, 12).map((a) => `${a.business_name ?? a.fq}: ${a.description}`).join("\n").slice(0, 2000);

  return <div className="stack">
    <Notice tone="info">A source is a connection to data. A context records a particular business purpose, question, and relevant metrics over that data. Several contexts may use the same source. Every investigation keeps its own question and pins the context version it used.</Notice>
    <div className="context-layout">
      <Card title="Analysis contexts" actions={canEdit && <button type="button" className="btn btn-sm btn-primary" onClick={() => choose(null)}>New context</button>}>
        <ErrorBox error={contexts.error} onRetry={contexts.reload} />
        {contexts.loading && !contexts.data && <Loading />}
        {contexts.data && latest.length === 0 && <EmptyState title="No contexts yet">Create a purpose for a source, then publish it for investigations.</EmptyState>}
        <ul className="list selectable" aria-label="Analysis contexts">
          {latest.map((c) => <li key={c.id}><button type="button" className={`list-button ${selected === c.id ? "active" : ""}`}
            onClick={() => choose(c)}><strong>{c.title ?? c.key}</strong><span className="muted small">v{c.version} · <StatusBadge status={c.status} /></span>
            <span className="muted small">{contextSpec(c).purpose}</span></button></li>)}
        </ul>
      </Card>
      <Card title={current ? `${current.title ?? current.key} · v${current.version}` : "New analysis context"}>
        {!canEdit && !current ? <p className="muted">Choose a context to see its business purpose.</p> : <form className="form" onSubmit={(e) => void save(e)}>
          <Field label="Name" htmlFor="context-title"><input id="context-title" value={title} disabled={!canEdit || !editable}
            onChange={(e) => setTitle(e.target.value)} required maxLength={300} placeholder="Revenue retention" /></Field>
          <Field label="Business purpose" htmlFor="context-purpose"><textarea id="context-purpose" value={spec.purpose} disabled={!canEdit || !editable}
            onChange={(e) => setSpec({ ...spec, purpose: e.target.value })} required minLength={10} maxLength={500} rows={2}
            placeholder="Understand which customer segments are losing recurring revenue" /></Field>
          <Field label="Business description" htmlFor="context-business"><textarea id="context-business" value={spec.business_description} disabled={!canEdit || !editable}
            onChange={(e) => setSpec({ ...spec, business_description: e.target.value })} maxLength={2000} rows={3}
            placeholder="Definitions, audience and assumptions for this purpose" /></Field>
          {canEdit && editable && <button type="button" className="btn btn-xs btn-ghost" disabled={!catalogNotes}
            onClick={() => { setSpec({ ...spec, business_description: catalogNotes }); setNote("Copied catalog descriptions. Review and edit them before saving."); }}>
            Use catalog descriptions as a draft</button>}
          <Field label="Starting question" htmlFor="context-question" hint="The analyst can edit this for each investigation."><textarea id="context-question"
            value={spec.question_template} disabled={!canEdit || !editable} onChange={(e) => setSpec({ ...spec, question_template: e.target.value })}
            required minLength={10} maxLength={1000} rows={2} /></Field>
          <fieldset className="schema-fieldset"><legend>Sources used by this context</legend>
            {(sources.data ?? []).map((s) => <label key={s.id} className="check"><input type="checkbox" disabled={!canEdit || !editable}
              checked={spec.source_ids.includes(s.id)} onChange={(e) => setSpec({ ...spec,
                source_ids: e.target.checked ? [...spec.source_ids, s.id] : spec.source_ids.filter((id) => id !== s.id) })} />{s.name} <span className="muted small">({s.status})</span></label>)}
          </fieldset>
          <Field label="Relevant metrics" htmlFor="context-metrics" hint="Optional metric names, separated by commas. Metric definitions and approval stay in Data → Metrics.">
            <input id="context-metrics" value={spec.metric_names.join(", ")} disabled={!canEdit || !editable}
              onChange={(e) => setSpec({ ...spec, metric_names: e.target.value.split(",").map((x) => x.trim()) })} /></Field>
          {current && !editable && <p className="small muted">Published versions are frozen. Create a revision to change this context.</p>}
          {current && editable && dirty && <p className="small muted">Save these changes before publishing this version.</p>}
          {note && <Notice tone="success">{note}</Notice>}
          <ErrorBox error={act.error} />
          <div className="form-actions">
            {canEdit && editable && <button type="submit" className="btn btn-primary" disabled={act.busy || spec.source_ids.length === 0}>Save draft</button>}
            {canEdit && current && editable && <button type="button" className="btn btn-success" disabled={act.busy || dirty} onClick={() => void publish()}>Publish reviewed version</button>}
            {canEdit && current && !editable && <button type="button" className="btn" onClick={() => { setKey(current.key); setSelected(null); setNote("New revision: review changes before saving and publishing."); }}>Create revision</button>}
          </div>
        </form>}
        <p className="small muted">Catalog descriptions and semantic model changes can be AI proposed. A person edits and reviews them in <Link to={to.data(wsId, "review")}>Review queue</Link> and <Link to={to.data(wsId, "definitions")}>Definitions</Link>; this context is published by a person.</p>
      </Card>
    </div>
  </div>;
}
