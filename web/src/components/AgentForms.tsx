import { useEffect, useId, useState, type FormEvent } from "react";
import { api, type AgentFormInput, type DefinitionVersion } from "../api";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast } from "../routes";
import { Drawer } from "./Drawer";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge } from "./ui";

type Editor = { mode: "new" | "edit" | "copy"; id?: string };
type Section = NonNullable<NonNullable<AgentFormInput["knowledge"]>["sections"]>[number];
const KEY = /^agent\.[a-z][a-z0-9_]{1,59}$/;
const PII_LABEL: Record<string, string> = { none: "No personal data", restricted: "Masked personal data", allowed: "Personal data" };

export async function agentDefinitions(wsId: string, status?: string): Promise<DefinitionVersion[]> {
  const result: DefinitionVersion[] = [];
  let cursor: string | null = null;
  do {
    const page = await api.listDefinitions(wsId, { kind: "agent", ...(status ? { status } : {}), ...(cursor ? { cursor } : {}) });
    result.push(...page.items.filter((d) => d.kind === "agent"));
    cursor = page.next_cursor;
  } while (cursor);
  return result;
}

const blank = (): AgentFormInput => ({
  key: "agent.", title: "", purpose: "", capabilities: [], default_actions: [],
  knowledge: { sections: [], budget_chars: null }, output: { type: "agent_output", name: "" },
  budget: { llm_calls: 2, usd: 0.05, queries: 0, max_steps: 3 },
  autonomy: { mode: "deterministic", pii_access: "none", max_rows: 1000 },
});

/**
 * Workspace agents (P7-19): owners describe a declarative agent in a form; the server compiles it into the
 * same Agent manifest the YAML path loads and refuses any grant beyond the workspace's. Published versions
 * are what workflow steps use.
 */
export function AgentsPanel({ wsId, role, onPublished }: { wsId: string; role: string | undefined; onPublished?: () => void }) {
  const defs = useAsync(() => agentDefinitions(wsId), [wsId]);
  const [editor, setEditor] = useState<Editor | null>(null);
  const publish = useAction();
  const canAuthor = roleAtLeast(role, "owner");
  const doPublish = async (d: DefinitionVersion) => {
    const row = await publish.run(() => api.publishDefinition(wsId, d.id, d.revision));
    if (row) { void defs.reload(); onPublished?.(); }
  };
  return <Card title="Agents" actions={canAuthor && <button type="button" className="btn btn-sm btn-primary" onClick={() => setEditor({ mode: "new" })}>Create agent</button>}>
    <p className="small muted">A workspace agent uses only capabilities enabled here, within the workspace budget and data policy. The server checks every grant; publish a version to use it in a workflow step.</p>
    <ErrorBox error={defs.error ?? publish.error} onRetry={defs.reload} />
    {defs.loading && !defs.data && <Loading />}
    {defs.data?.length === 0 && <EmptyState title="No workspace agents yet">{canAuthor ? "Create one from a form." : "A workspace owner can create one."}</EmptyState>}
    {!!defs.data?.length && <ul className="list" aria-label="Workspace agents">
      {defs.data.map((d) => <li key={d.id} className="list-item">
        <div><strong>{d.title || d.key}</strong> <span className="small muted">{d.key} · v{d.version}</span>
          <div><StatusBadge status={d.status} /></div></div>
        {canAuthor && <div className="chip-row">
          {d.status === "draft" && <button type="button" className="btn btn-xs" onClick={() => setEditor({ mode: "edit", id: d.id })}>Edit {d.key}</button>}
          {d.status === "draft" && <button type="button" className="btn btn-xs btn-primary" disabled={publish.busy} onClick={() => void doPublish(d)}>Publish {d.key}</button>}
          {d.status === "published" && <button type="button" className="btn btn-xs" onClick={() => setEditor({ mode: "copy", id: d.id })}>New version of {d.key}</button>}
        </div>}
      </li>)}
    </ul>}
    {editor && <AgentEditor wsId={wsId} editor={editor} onClose={() => setEditor(null)}
      onSaved={() => { setEditor(null); void defs.reload(); }} />}
  </Card>;
}

function AgentEditor({ wsId, editor, onClose, onSaved }: { wsId: string; editor: Editor; onClose: () => void; onSaved: () => void }) {
  const id = useId();
  const options = useAsync(() => api.agentFormOptions(wsId), [wsId]);
  const original = useAsync(() => editor.id ? api.getDefinition(wsId, editor.id) : Promise.resolve(null), [wsId, editor.id]);
  const act = useAction();
  const [form, setForm] = useState<AgentFormInput>(blank);
  const source = original.data;
  useEffect(() => { if (source?.form) setForm(source.form); }, [source]);
  const set = (patch: Partial<AgentFormInput>) => setForm((f) => ({ ...f, ...patch }));
  const budget = { llm_calls: 2, usd: 0.05, queries: 0, max_steps: 3, ...form.budget };
  const autonomy = { mode: "deterministic", pii_access: "none", max_rows: 1000, ...form.autonomy } as const;
  const knowledge = { sections: [] as Section[], budget_chars: null as number | null, ...form.knowledge };
  const defaults = form.default_actions ?? [];
  const limits = options.data?.limits;
  const caps = options.data?.capabilities ?? [];
  const toggleCap = (capId: string, on: boolean) => set({
    capabilities: on ? [...form.capabilities, capId] : form.capabilities.filter((c) => c !== capId),
    default_actions: on ? defaults : defaults.filter((c) => c !== capId),
  });
  const problem = !KEY.test(form.key) ? "Use a key like agent.table_notes (lowercase letters, digits, _)."
    : form.title.trim().length < 3 ? "Give the agent a name."
      : form.purpose.trim().length < 10 ? "Describe the agent's purpose in at least 10 characters."
        : form.capabilities.length === 0 ? "Allow at least one capability."
          : autonomy.mode === "deterministic" && defaults.length === 0 ? "Without model proposals, choose at least one default action."
            : form.output.name.trim().length < 3 ? "Name the output the agent writes."
              : null;
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (problem) return;
    const body: AgentFormInput = { ...form, title: form.title.trim(), purpose: form.purpose.trim(), output: { ...form.output, name: form.output.name.trim() } };
    const row = await act.run(() => editor.mode === "edit" && source
      ? api.updateAgentFromForm(wsId, source.id, source.revision, body)
      : api.createAgentFromForm(wsId, body));
    if (row) onSaved();
  };
  const loading = !options.data || (editor.id && !original.data);
  return <Drawer title={editor.mode === "new" ? "Create agent" : editor.mode === "copy" ? "New agent version" : "Edit agent"} onClose={onClose} className="drawer-wide">
    <ErrorBox error={options.error ?? original.error} />
    {loading && <Loading />}
    {source && !source.form && <Notice tone="warning">This agent uses manifest fields the form cannot show; edit it through the definitions API.</Notice>}
    {!loading && (!source || source.form) && <form className="form" onSubmit={save} aria-label="Agent form">
      <Field label="Name" htmlFor={`${id}-title`}><input id={`${id}-title`} value={form.title} onChange={(e) => set({ title: e.target.value })} /></Field>
      <Field label="Key" htmlFor={`${id}-key`} hint="A stable identifier; workflow steps use it and versions keep it.">
        <input id={`${id}-key`} value={form.key} disabled={editor.mode !== "new"} onChange={(e) => set({ key: e.target.value })} /></Field>
      <Field label="Purpose" htmlFor={`${id}-purpose`} hint="What the agent is for; it cannot change what the agent is allowed to do.">
        <textarea id={`${id}-purpose`} rows={2} value={form.purpose} onChange={(e) => set({ purpose: e.target.value })} /></Field>
      <fieldset className="schema-fieldset"><legend>Allowed capabilities</legend>
        <p className="small muted">Only capabilities enabled in this workspace can be granted. Tools follow from the capabilities.</p>
        {caps.map((c) => <div key={c.id}>
          <label className="check"><input type="checkbox" disabled={!c.grantable && !form.capabilities.includes(c.id)}
            checked={form.capabilities.includes(c.id)} onChange={(e) => toggleCap(c.id, e.target.checked)} />{c.id}
            <span className="muted small">— {c.summary}</span></label>
          {!c.grantable && <div className="small warn-text">{c.reason}</div>}
          {form.capabilities.includes(c.id) && c.default_input && <label className="check small">
            <input type="checkbox" checked={defaults.includes(c.id)} onChange={(e) => set({
              default_actions: e.target.checked ? [...defaults, c.id] : defaults.filter((x) => x !== c.id) })} />
            Run {c.id} by default</label>}
        </div>)}
      </fieldset>
      <fieldset className="schema-fieldset"><legend>Knowledge scope</legend>
        {(options.data?.knowledge_sections ?? []).map((k) => k as Section).map((k) => <label key={k} className="check"><input type="checkbox"
          checked={knowledge.sections.includes(k)} onChange={(e) => set({ knowledge: { ...knowledge,
            sections: e.target.checked ? [...knowledge.sections, k] : knowledge.sections.filter((x) => x !== k) } })} />{k.replace(/_/g, " ")}</label>)}
      </fieldset>
      <Field label="Output name" htmlFor={`${id}-output`} hint="The agent writes one agent output artifact with its results.">
        <input id={`${id}-output`} value={form.output.name} onChange={(e) => set({ output: { ...form.output, name: e.target.value } })} /></Field>
      <fieldset className="schema-fieldset"><legend>Autonomy</legend>
        <Field label="Mode" htmlFor={`${id}-mode`}><select id={`${id}-mode`} value={autonomy.mode}
          onChange={(e) => set({ autonomy: { ...autonomy, mode: e.target.value as "deterministic" | "propose" } })}>
          <option value="deterministic">Default actions only (no model)</option>
          <option value="propose">A model proposes actions; code validates each one</option></select></Field>
        <Field label="Data access" htmlFor={`${id}-pii`}><select id={`${id}-pii`} value={autonomy.pii_access}
          onChange={(e) => set({ autonomy: { ...autonomy, pii_access: e.target.value as "none" | "restricted" | "allowed" } })}>
          {(limits?.pii_access ?? ["none"]).map((p) => <option key={p} value={p}>{PII_LABEL[p] ?? p}</option>)}</select></Field>
        <Field label="Maximum rows" htmlFor={`${id}-rows`}><input id={`${id}-rows`} type="number" min={1} max={limits?.max_rows}
          value={autonomy.max_rows} onChange={(e) => set({ autonomy: { ...autonomy, max_rows: Number(e.target.value) } })} /></Field>
      </fieldset>
      <fieldset className="schema-fieldset"><legend>Budget per step</legend>
        <div className="form-grid">
          <Field label="Model calls" htmlFor={`${id}-calls`}><input id={`${id}-calls`} type="number" min={0} max={limits?.llm_calls}
            disabled={autonomy.mode === "deterministic"} value={budget.llm_calls}
            onChange={(e) => set({ budget: { ...budget, llm_calls: Number(e.target.value) } })} /></Field>
          <Field label="Model cost (USD)" htmlFor={`${id}-usd`}><input id={`${id}-usd`} type="number" min={0} step={0.01} max={limits?.usd}
            value={budget.usd} onChange={(e) => set({ budget: { ...budget, usd: Number(e.target.value) } })} /></Field>
          <Field label="Source queries" htmlFor={`${id}-queries`}><input id={`${id}-queries`} type="number" min={0} max={limits?.queries}
            value={budget.queries} onChange={(e) => set({ budget: { ...budget, queries: Number(e.target.value) } })} /></Field>
          <Field label="Rounds" htmlFor={`${id}-steps`}><input id={`${id}-steps`} type="number" min={1} max={limits?.max_steps}
            value={budget.max_steps} onChange={(e) => set({ budget: { ...budget, max_steps: Number(e.target.value) } })} /></Field>
        </div>
      </fieldset>
      {problem && <p className="small warn-text" role="status">{problem}</p>}
      <ErrorBox error={act.error} />
      <div className="form-actions"><button type="submit" className="btn btn-primary" disabled={!!problem || act.busy}>Save agent draft</button></div>
    </form>}
  </Drawer>;
}
