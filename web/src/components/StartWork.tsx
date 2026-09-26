import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Source, type WorkspaceDetail } from "../api";
import { jobKindsFromCapabilities, type JobKind, type JobKindId } from "../lib/jobKinds";
import { useAction, useAsync } from "../lib/hooks";
import { autonomyInWords } from "../lib/status";
import { to } from "../routes";
import { Drawer } from "./Drawer";
import { ErrorBox, Field, KeyValue, Loading, Notice, TechnicalDetails, Value } from "./ui";

/** The job kinds for a workspace: the registry, the caller's role and whether any source is ready. */
export function useJobKinds(wsId: string, ws: WorkspaceDetail | undefined, sources: Source[] | undefined) {
  const caps = useAsync(() => api.listCapabilities({ workspace_id: wsId }), [wsId]);
  const kinds = caps.data && ws && sources
    ? jobKindsFromCapabilities(caps.data.capabilities, { readySources: sources.filter((s) => s.status === "ready").length, role: ws.role })
    : undefined;
  return { kinds, error: caps.error, reload: caps.reload };
}

interface Draft { kind?: JobKindId; objective?: string; sourceId?: string }
const draftKey = (ws: string) => `analystos.startWork.${ws}`;

/** The unsent brief survives a reload or a failed start (workbench-ux §2); storage may be unavailable. */
function loadDraft(ws: string): Draft {
  try {
    return JSON.parse(window.localStorage.getItem(draftKey(ws)) ?? "{}") as Draft;
  } catch {
    return {};
  }
}
function saveDraft(ws: string, d: Draft | null) {
  try {
    if (d) window.localStorage.setItem(draftKey(ws), JSON.stringify(d));
    else window.localStorage.removeItem(draftKey(ws));
  } catch {
    /* storage blocked: the draft lives for this session only */
  }
}

/** The primary action: opens Start work. */
export function StartWorkButton({ wsId, className = "btn btn-primary" }: { wsId: string; className?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button type="button" ref={ref} className={className} aria-haspopup="dialog" onClick={() => setOpen(true)}>Start work</button>
      {open && <StartWorkDialog wsId={wsId} onClose={() => { setOpen(false); ref.current?.focus(); }} />}
    </>
  );
}

/**
 * Start work (spec v4 §15): the job kinds from the capability registry. A kind that cannot run is
 * shown with its reason and has no start button, so nothing sends a predictably failing request.
 */
export function StartWorkDialog({ wsId, onClose, initialKind }: { wsId: string; onClose: () => void; initialKind?: JobKindId }) {
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const { kinds, error } = useJobKinds(wsId, ws.data, sources.data);
  const [draft, setDraft] = useState<Draft>(() => ({ ...loadDraft(wsId), ...(initialKind ? { kind: initialKind } : {}) }));
  const nav = useNavigate();
  const chosen = kinds?.find((k) => k.id === draft.kind && k.enabled);

  const update = (patch: Draft) => setDraft((d) => {
    const next = { ...d, ...patch };
    saveDraft(wsId, next);
    return next;
  });

  const pick = (k: JobKind) => {
    if (!k.enabled) return;
    if (k.action === "prepare") {
      onClose();
      nav(to.work(wsId, "prepare"));
      return;
    }
    if (k.action === "monitor") {
      onClose();
      nav(to.monitoring(wsId, { tab: "monitors", new: "1" } as { tab: string }));
      return;
    }
    update({ kind: k.id, objective: draft.objective || k.objectivePrefix || ws.data?.objective || "" });
  };

  return (
    <Drawer title={chosen ? `Start work: ${chosen.label}` : "Start work"} onClose={onClose} className="drawer-wide">
      <ErrorBox error={ws.error ?? sources.error ?? error} />
      {!kinds && !error && <Loading />}
      {kinds && !chosen && (
        <>
          <p className="small muted">What do you want to do? Choices that cannot run here say what they need.</p>
          <ul className="job-kinds" aria-label="Job kinds">
            {kinds.map((k) => (
              <li key={k.id} className={`job-kind ${k.enabled ? "" : "job-kind-disabled"}`}>
                {k.enabled ? (
                  <button type="button" className="job-kind-button" onClick={() => pick(k)}>
                    <strong>{k.label}</strong>
                    <span className="small muted block">{k.description}</span>
                  </button>
                ) : (
                  <div className="job-kind-button" aria-disabled="true">
                    <strong>{k.label}</strong> <span className="tag tag-neutral">not available</span>
                    <span className="small muted block">{k.description}</span>
                    <span className="small block job-kind-reason">{k.reason}</span>
                  </div>
                )}
              </li>
            ))}
          </ul>
          <TechnicalDetails value={kinds.map((k) => ({ kind: k.id, enabled: k.enabled, uses: k.uses }))} label="Technical details" />
        </>
      )}
      {chosen && ws.data && sources.data && (
        <InvestigationForm kind={chosen} ws={ws.data} sources={sources.data} draft={draft} update={update}
          onBack={() => update({ kind: undefined })}
          onStarted={(runId) => { saveDraft(wsId, null); onClose(); nav(to.run(wsId, runId)); }} />
      )}
    </Drawer>
  );
}

/** The brief and a preflight: what will be read, the limits it runs under, and what needs a person. */
function InvestigationForm({ kind, ws, sources, draft, update, onBack, onStarted }: {
  kind: JobKind; ws: WorkspaceDetail; sources: Source[]; draft: Draft; update: (d: Draft) => void; onBack: () => void; onStarted: (runId: string) => void;
}) {
  const id = useId();
  const act = useAction();
  const ready = sources.filter((s) => s.status === "ready");
  const objective = draft.objective ?? "";
  const tooShort = objective.trim().length < 10;
  const needsSource = ready.length > 1 && !draft.sourceId;
  useEffect(() => {
    if (ready.length === 1 && draft.sourceId !== ready[0].id) update({ sourceId: ready[0].id });
  }, [ready.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (tooShort || needsSource) return;
    const run = await act.run(() => api.startRun(ws.id, { objective: objective.trim(), source_ids: draft.sourceId ? [draft.sourceId] : undefined }));
    if (run) onStarted(run.id);
  };
  const p = ws.policy ?? {};
  const reading = draft.sourceId ? ready.filter((s) => s.id === draft.sourceId) : ready;
  return (
    <form className="form" onSubmit={submit} aria-label={`Start ${kind.label.toLowerCase()}`}>
      <p className="small muted">{kind.description}</p>
      <Field label="What should the investigation find out?" htmlFor={`${id}-obj`} hint="A question or goal, at least 10 characters.">
        <textarea id={`${id}-obj`} rows={3} value={objective} onChange={(e) => update({ objective: e.target.value })} aria-invalid={tooShort} />
      </Field>
      {ready.length > 1 && (
        <Field label="Data" htmlFor={`${id}-src`} hint="One source per investigation in this release.">
          <select id={`${id}-src`} value={draft.sourceId ?? ""} onChange={(e) => update({ sourceId: e.target.value })}>
            <option value="">Choose a source…</option>
            {ready.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </Field>
      )}
      <section className="preflight" aria-labelledby={`${id}-pf`}>
        <h3 id={`${id}-pf`}>Before it starts</h3>
        <KeyValue items={[
          ["Reads", reading.length ? reading.map((s) => s.name).join(", ") : "no source chosen yet"],
          ["Spends at most", <span key="b"><Value value={p.run_cost_budget_usd} format="usd" /> per investigation</span>],
          ["Checks", "Every finding is re-tested before it is reported; unverified results are labelled."],
          ["Needs a person for", p.publish_requires_approval === false ? "nothing outside the platform by default" : "anything published or sent outside the platform"],
          ["How much it does alone", autonomyInWords(ws.autonomy_level)],
        ]} />
      </section>
      {tooShort && objective.length > 0 && <p className="warn-text small" role="status">Say a little more: at least 10 characters.</p>}
      {needsSource && <Notice tone="info">Choose which source to read.</Notice>}
      <ErrorBox error={act.error} />
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" onClick={onBack}>Back</button>
        <button type="submit" className="btn btn-primary" disabled={act.busy || tooShort || needsSource}>
          {act.busy ? "Starting…" : "Start investigation"}</button>
      </div>
    </form>
  );
}
