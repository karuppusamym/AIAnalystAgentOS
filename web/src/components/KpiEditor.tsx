import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, type MetricProblem, type MetricValidation, type SemanticMetric } from "../api";
import { useAuth } from "../auth";
import { EMPTY_KPI, kpiBody, localKpiProblems, problemsByField, type KpiDraft } from "../lib/build";
import { fmtDate, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";
import { Card, CodeBlock, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge, Tag } from "./ui";

const VALIDATE_DEBOUNCE_MS = 350;
const FORMATS = ["", "number", "percent", "hours", "currency"];

/**
 * Build → KPIs (P4-U05): the workspace semantic layer (P4-K03) as an editor. A KPI is proposed with
 * live validation (the server's SEM-003 shape rule and SEM-005 conflicts, not a client guess), then
 * approved or rejected through the semantic approve route, where separation of duties holds: the
 * proposer can never decide their own KPI.
 */
export function KpiEditor({ wsId, selected, onSelect }: { wsId: string; selected: string | null; onSelect: (name: string | null) => void }) {
  const model = useAsync(() => api.semanticModel(wsId), [wsId]);
  const metrics = useMemo(() => [...(model.data?.metrics ?? [])].sort((a, b) => a.name.localeCompare(b.name)), [model.data]);
  return (
    <div className="stack">
      <Card title="Propose a KPI">
        <KpiForm wsId={wsId} onProposed={(name) => { void model.reload(); onSelect(name); }} />
      </Card>
      <ErrorBox error={model.error} onRetry={model.reload} />
      {!!model.data?.conflicts.length && (
        <Notice tone="warning">
          <strong>{model.data.conflicts.length} conflict{model.data.conflicts.length > 1 ? "s" : ""} in the semantic model</strong>
          <ul className="small">{model.data.conflicts.map((c) => <li key={`${c.kind}:${c.names.join(",")}`}>{c.detail}</li>)}</ul>
        </Notice>
      )}
      {model.loading && !model.data && <Loading />}
      {model.data && metrics.length === 0 && <EmptyState title="No KPIs yet">Propose one above, or promote one from an Ask answer.</EmptyState>}
      {metrics.length > 0 && (
        <div className="split">
          <div className="split-list">
            <ul className="list selectable" aria-label="KPIs">
              {metrics.map((m) => (
                <li key={m.name}>
                  <button type="button" className={`list-button ${m.name === selected ? "active" : ""}`} onClick={() => onSelect(m.name)}
                    aria-current={m.name === selected ? "true" : undefined}>
                    <span className="list-button-head"><span className="clamp-1">{m.display_name || m.name}</span><StatusBadge status={m.status} /></span>
                    <span className="chip-row small"><code>{m.name}</code><span className="muted">v{m.version}</span></span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div className="split-detail">
            {selected ? <KpiDetail wsId={wsId} name={selected} onDecided={() => void model.reload()} />
              : <EmptyState title="Select a KPI">Versions, approval state and the definition an approver signs.</EmptyState>}
          </div>
        </div>
      )}
      <p className="muted small">Ossie {model.data?.ossie_version ?? "—"}. Approved KPIs are what builds materialize and what publication requires.</p>
    </div>
  );
}

function serverProblems(err: unknown): MetricProblem[] {
  if (err instanceof ApiError && Array.isArray((err.details as { problems?: unknown }).problems)) {
    return (err.details as { problems: MetricProblem[] }).problems;
  }
  return [];
}

function KpiForm({ wsId, onProposed }: { wsId: string; onProposed: (name: string) => void }) {
  const id = useId();
  const [draft, setDraft] = useState<KpiDraft>(EMPTY_KPI);
  const [touched, setTouched] = useState(false);
  const [check, setCheck] = useState<MetricValidation | null>(null);
  const [checking, setChecking] = useState(false);
  const [submitProblems, setSubmitProblems] = useState<MetricProblem[]>([]);
  const [done, setDone] = useState<{ name: string; version: number; approval: string | null; created: boolean } | null>(null);
  const act = useAction();
  const local = localKpiProblems(draft);

  // Live validation against the server once the draft is locally well-formed (debounced).
  useEffect(() => {
    if (!touched || local.length) {
      setCheck(null);
      return;
    }
    let live = true;
    setChecking(true);
    const t = setTimeout(() => {
      api.validateMetric(wsId, kpiBody(draft))
        .then((r) => { if (live) setCheck(r); })
        .catch(() => { if (live) setCheck(null); })
        .finally(() => { if (live) setChecking(false); });
    }, VALIDATE_DEBOUNCE_MS);
    return () => {
      live = false;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsId, touched, JSON.stringify(draft)]);

  const problems = problemsByField([...(touched ? local : []), ...(check?.problems ?? []), ...submitProblems]);
  const set = (k: keyof KpiDraft) => (e: { target: { value: string } }) => {
    setTouched(true);
    setSubmitProblems([]);
    setDone(null);
    setDraft((d) => ({ ...d, [k]: e.target.value }));
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (local.length) return;
    const r = await act.run(async () => {
      try {
        return await api.proposeMetric(wsId, kpiBody(draft));
      } catch (err) {
        setSubmitProblems(serverProblems(err));
        throw err;
      }
    });
    if (r) {
      setDone({ name: r.metric.name, version: r.metric.version, approval: r.metric.approval_id, created: r.created });
      setDraft(EMPTY_KPI);
      setTouched(false);
      setCheck(null);
      onProposed(r.metric.name);
    }
  };
  const field = (k: string) => ({
    "aria-invalid": problems[k] ? true : undefined,
    "aria-describedby": problems[k] ? `${id}-${k}-err` : undefined,
  });
  const err = (k: string) => problems[k] ? <span id={`${id}-${k}-err`} className="field-error" role="alert">{problems[k]}</span> : null;
  return (
    <form className="form" onSubmit={submit} aria-label="Propose a KPI" noValidate>
      <div className="form-row">
        <Field label="Name" htmlFor={`${id}-name`} hint="Letters, digits and underscores, e.g. p1_breach_rate">
          <input id={`${id}-name`} value={draft.name} onChange={set("name")} autoComplete="off" {...field("name")} />
          {err("name")}
        </Field>
        <Field label="Display name" htmlFor={`${id}-display`}>
          <input id={`${id}-display`} value={draft.display_name} onChange={set("display_name")} />
        </Field>
        <Field label="Format" htmlFor={`${id}-format`}>
          <select id={`${id}-format`} value={draft.format} onChange={set("format")}>
            {FORMATS.map((f) => <option key={f} value={f}>{f || "—"}</option>)}
          </select>
        </Field>
        <Field label="Grain" htmlFor={`${id}-grain`} hint="e.g. week">
          <input id={`${id}-grain`} value={draft.grain} onChange={set("grain")} />
        </Field>
      </div>
      <Field label="Expression" htmlFor={`${id}-expr`} hint="One aggregate SQL expression, no subqueries, e.g. AVG(resolution_hours)">
        <textarea id={`${id}-expr`} rows={2} className="mono" value={draft.expression} onChange={set("expression")} {...field("expression")} />
        {err("expression")}
      </Field>
      <div className="form-row">
        <Field label="Dimensions" htmlFor={`${id}-dims`} hint="Comma-separated">
          <input id={`${id}-dims`} value={draft.dimensions} onChange={set("dimensions")} />
        </Field>
        <Field label="Filters" htmlFor={`${id}-filters`} hint="Comma-separated SQL predicates">
          <input id={`${id}-filters`} value={draft.filters} onChange={set("filters")} />
        </Field>
      </div>
      <Field label="Description" htmlFor={`${id}-desc`}>
        <textarea id={`${id}-desc`} rows={2} value={draft.description} onChange={set("description")} />
      </Field>
      <div className="kpi-check" aria-live="polite">
        {checking && <span className="muted small">Checking…</span>}
        {!checking && check?.ok && !check.conflicts.length && !check.existing && <span className="small"><StatusBadge status="ok" label="valid" /> A well-formed aggregate with no conflicts.</span>}
        {!checking && check?.existing && (
          <span className="small">This exact definition is already v{check.existing.version} ({check.existing.status}); proposing it again changes nothing.</span>
        )}
        {!checking && !!check?.conflicts.length && (
          <ul className="small warn-list" aria-label="Conflicts">{check.conflicts.map((c) => <li key={c.kind}><Tag tone="warning">{c.kind.replace("_", " ")}</Tag> {c.detail}</li>)}</ul>
        )}
      </div>
      <ErrorBox error={act.error} />
      {done && (
        <Notice tone="success">
          {done.created ? <>Proposed <code>{done.name}</code> v{done.version}. </> : <>The definition was already recorded as <code>{done.name}</code> v{done.version}. </>}
          It waits for an approver who is not you{done.approval ? <> (approval <code>{done.approval}</code>)</> : null}.
        </Notice>
      )}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Proposing…" : "Propose KPI"}</button>
      </div>
    </form>
  );
}

function KpiDetail({ wsId, name, onDecided }: { wsId: string; name: string; onDecided: () => void }) {
  const versions = useAsync(() => api.metricVersions(wsId, name), [wsId, name]);
  const { user } = useAuth();
  if (versions.error) return <ErrorBox error={versions.error} onRetry={versions.reload} />;
  if (!versions.data) return <Loading />;
  const rows = [...versions.data].sort((a, b) => b.version - a.version);
  return (
    <div className="stack">
      {rows.map((m) => (
        <KpiVersion key={m.id} wsId={wsId} metric={m} mine={!!user && m.proposed_by === user.id}
          onDecided={() => { void versions.reload(); onDecided(); }} />
      ))}
    </div>
  );
}

function KpiVersion({ wsId, metric: m, mine, onDecided }: { wsId: string; metric: SemanticMetric; mine: boolean; onDecided: () => void }) {
  const [reason, setReason] = useState("");
  const act = useAction();
  const reasonId = useId();
  const def = m.definition as { description?: string; format?: string; grain?: string; dimensions?: string[]; filters?: string[] };
  const decide = async (approve: boolean) => {
    if (await act.run(() => api.decideMetric(wsId, m.name, approve, m.version, reason))) onDecided();
  };
  return (
    <Card title={<>{m.display_name || m.name} <span className="muted small">· v{m.version}</span></>} actions={<StatusBadge status={m.status} />}>
      {def.description && <p>{def.description}</p>}
      <CodeBlock code={m.expression} label="Expression" />
      <p className="chip-row small">
        {def.format && <Tag>{def.format}</Tag>}{def.grain && <Tag>grain: {def.grain}</Tag>}
        {(def.dimensions ?? []).map((d) => <Tag key={d} tone="info">{d}</Tag>)}
        <span className="muted">proposed via {m.proposed_via} · {fmtDate(m.created_at)} · definition <code title={m.content_hash}>{shortHash(m.content_hash, 10)}</code></span>
      </p>
      {m.reason && <p className="small muted">{m.reason}</p>}
      {m.decided_at && <p className="small muted">Decided {fmtDate(m.decided_at)}.</p>}
      {m.status === "proposed" && (
        mine ? (
          <p className="small" role="note">
            You proposed this version. Separation of duties: another approver decides it
            {m.approval_id ? <> (here or in the <Link to={to.approvals(wsId)}>approvals inbox</Link>)</> : null}.
          </p>
        ) : (
          <div className="approval-actions">
            <label className="sr-only" htmlFor={reasonId}>Reason for {m.name} v{m.version}</label>
            <input id={reasonId} placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
            <button type="button" className="btn btn-success btn-sm" disabled={act.busy} onClick={() => void decide(true)}>Approve v{m.version}</button>
            <button type="button" className="btn btn-danger btn-sm" disabled={act.busy} onClick={() => void decide(false)}>Reject</button>
          </div>
        )
      )}
      {m.status === "proposed" && <p className="muted small">Approving binds this exact definition's hash and deprecates the previously approved version.</p>}
      <ErrorBox error={act.error} />
    </Card>
  );
}
