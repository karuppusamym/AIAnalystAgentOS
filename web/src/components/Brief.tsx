import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type Assertion, type ReadinessAssessment, type WorkspaceBrief } from "../api";
import { fmtDate, fmtValue } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { CHECK_WORDS, READINESS_WORDS } from "../lib/jobKinds";
import { roleAtLeast, to } from "../routes";
import { isStaleEdit, StaleEditNotice } from "./StaleEdit";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge, Tag, TechnicalDetails } from "./ui";

/** Brief groups and their fields in plain words (contracts/brief.py GROUP_FIELDS). */
export const BRIEF_GROUPS: { id: string; label: string; fields: Record<string, string> }[] = [
  { id: "decision", label: "The decision", fields: { question: "Question", owner: "Owner", intended_action: "Intended action", audience: "Audience",
    acceptance_rubric: "What counts as a good answer" } },
  { id: "domain", label: "Glossary", fields: { term: "Term", entity: "Entity", alias: "Alias", prohibited_interpretation: "Must not be read as" } },
  { id: "data_semantics", label: "What a row means", fields: { grain: "Grain (one row per…)", entity_key: "Entity key",
    join_cardinality: "Join cardinality", dedup_rule: "De-duplication rule", exclusion_rule: "Exclusion rule" } },
  { id: "time_measures", label: "Time and measures", fields: { event_time: "Event time", availability_time: "Known at", timezone: "Time zone",
    fiscal_calendar: "Fiscal calendar", unit: "Unit", currency: "Currency", numerator: "Numerator", denominator: "Denominator",
    aggregation: "Aggregation" } },
  { id: "ml_objective", label: "Prediction target", fields: { target: "Target", prediction_moment: "Predicted at", horizon: "Horizon",
    label_availability: "When labels are known", error_costs: "Cost of errors", evaluation_metric: "Evaluation metric" } },
  { id: "constraints", label: "Constraints", fields: { source_scope: "Sources", destination_scope: "Destinations",
    freshness_tolerance_hours: "Freshness tolerance (hours)", residency: "Residency", retention: "Retention", compute_budget: "Compute budget",
    query_budget: "Query budget" } },
  { id: "knowledge", label: "Knowledge", fields: { context_ids: "Context documents", metric_versions: "Metric versions",
    accepted_corrections: "Accepted corrections" } },
];

const ORIGIN_WORDS: Record<string, { label: string; tone: string; title: string }> = {
  source: { label: "from the source", tone: "info", title: "Read from source metadata or an approved definition" },
  rule: { label: "inferred by a rule", tone: "neutral", title: "Inferred by deterministic code over profiles and relationships" },
  model: { label: "suggested by a model", tone: "jev", title: "Proposed by a model; a person or a check must confirm it" },
  user: { label: "stated by a person", tone: "success", title: "Stated or reviewed by a person: never overwritten by a refresh" },
};

const REVIEW_WORDS: Record<string, { label: string; status: string }> = {
  suggested: { label: "open question", status: "pending" },
  reviewed: { label: "reviewed", status: "approved" },
  validated: { label: "validated by a check", status: "verified" },
  rejected: { label: "rejected", status: "rejected" },
};

export function fieldLabel(group: string, field: string): string {
  return BRIEF_GROUPS.find((g) => g.id === group)?.fields[field] ?? field.replace(/_/g, " ");
}

/** Only reviewed or validated assertions are facts; a suggestion is a question until a person or a check confirms it. */
export const isFact = (a: Assertion) => a.review_state === "reviewed" || a.review_state === "validated";

export function OriginTag({ origin }: { origin: string }) {
  const o = ORIGIN_WORDS[origin] ?? { label: origin, tone: "neutral", title: origin };
  return <span className={`tag tag-${o.tone}`} title={o.title}>{o.label}</span>;
}

// ------------------------------------------------------------------------------------ readiness
/**
 * A readiness assessment: the verdict in words and every check with its own status, reason and
 * remediation. There is deliberately no score: one failed required check decides, and advisory
 * checks are shown as they are.
 */
export function ReadinessResult({ assessment: a, onChooseAlternative }: {
  assessment: ReadinessAssessment; onChooseAlternative?: (jobKind: string) => void;
}) {
  const verdict = READINESS_WORDS[a.status] ?? { label: a.status, status: a.status };
  const required = a.checks.filter((c) => c.required);
  const advisory = a.checks.filter((c) => !c.required);
  return (
    <div className="readiness" aria-label={`Readiness for ${a.job_kind}`} role="group">
      <p className="readiness-verdict">
        <StatusBadge status={verdict.status} label={verdict.label} />{" "}
        <span className="small muted">decided by the required checks below; advisory checks never change it.</span>
      </p>
      {[["Required checks", required], ["Advisory checks", advisory]].map(([title, list]) => (list as typeof a.checks).length > 0 && (
        <div className="table-wrap" key={title as string}>
          <table className="table table-compact">
            <caption>{title as string}</caption>
            <thead><tr><th scope="col">Check</th><th scope="col">Result</th><th scope="col">Why</th><th scope="col">What to do</th></tr></thead>
            <tbody>
              {(list as typeof a.checks).map((c, k) => (
                <tr key={`${c.check}-${c.subject ?? ""}-${k}`}>
                  <td>{CHECK_WORDS[c.check] ?? c.check}{c.subject && <div className="muted small"><code>{c.subject}</code></div>}</td>
                  <td><StatusBadge status={c.status} label={c.status.replace(/_/g, " ")} /></td>
                  <td className="small">{c.reason}</td>
                  <td className="small">{c.remediation ?? <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {a.alternatives.length > 0 && (
        <div className="small">
          <p>Supported instead, if you choose it explicitly (nothing is switched for you):</p>
          <ul className="chip-row" aria-label="Alternatives">
            {a.alternatives.map((alt) => (
              <li key={alt.job_kind}>
                {onChooseAlternative
                  ? <button type="button" className="btn btn-xs" onClick={() => onChooseAlternative(alt.job_kind)}>Choose {alt.job_kind}</button>
                  : <Tag>{alt.job_kind}</Tag>}
                {alt.note && <span className="muted"> {alt.note}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      <TechnicalDetails value={{ id: a.id, brief_version: a.brief_version, inputs: a.inputs, inputs_hash: a.inputs_hash }} />
    </div>
  );
}

/** Check whether a job kind can run on chosen data (Data → Brief & readiness). */
export function ReadinessPanel({ wsId, initialKind = "explain" }: { wsId: string; initialKind?: string }) {
  const id = useId();
  const [kind, setKind] = useState(initialKind);
  const [assets, setAssets] = useState("");
  const [target, setTarget] = useState("");
  const [timeColumn, setTimeColumn] = useState("");
  const [result, setResult] = useState<ReadinessAssessment | null>(null);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.assessReadiness(wsId, {
      job_kind: kind, assets: assets.split(",").map((s) => s.trim()).filter(Boolean), target: target.trim() || null,
      time_column: timeColumn.trim() || null, measures: [],
    }));
    if (r) setResult(r);
  };
  return (
    <Card title="Check readiness">
      <p className="muted small">Before starting, see whether a kind of work can run on this data, and what to fix if not.</p>
      <form className="form" onSubmit={submit} aria-label="Check readiness">
        <div className="form-row">
          <Field label="Kind of work" htmlFor={`${id}-kind`}>
            <select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value)}>
              {["explain", "compare", "forecast", "predict", "prepare", "monitor"].map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          </Field>
          <Field label="Tables" htmlFor={`${id}-assets`} hint="schema.table, comma separated; empty uses your whole scope.">
            <input id={`${id}-assets`} value={assets} onChange={(e) => setAssets(e.target.value)} placeholder="stg_sn.incident" />
          </Field>
        </div>
        {(kind === "predict" || kind === "forecast") && (
          <div className="form-row">
            <Field label="Target column" htmlFor={`${id}-target`}>
              <input id={`${id}-target`} value={target} onChange={(e) => setTarget(e.target.value)} />
            </Field>
            <Field label="Time column" htmlFor={`${id}-time`}>
              <input id={`${id}-time`} value={timeColumn} onChange={(e) => setTimeColumn(e.target.value)} />
            </Field>
          </div>
        )}
        <div className="form-actions">
          <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy}>{act.busy ? "Checking…" : "Check readiness"}</button>
        </div>
      </form>
      <ErrorBox error={act.error} />
      {result && <ReadinessResult assessment={result} onChooseAlternative={setKind} />}
    </Card>
  );
}

// ------------------------------------------------------------------------------------ the brief
function AssertionItem({ a, canReview, busy, onReview, onReject }: {
  a: Assertion; canReview: boolean; busy: boolean; onReview: () => void; onReject: () => void;
}) {
  const r = REVIEW_WORDS[a.review_state] ?? { label: a.review_state, status: a.review_state };
  const name = `${fieldLabel(a.group, a.field)}${a.subject ? ` of ${a.subject}` : ""}`;
  return (
    <li className="assertion" aria-label={name}>
      <div className="assertion-head">
        <strong>{fieldLabel(a.group, a.field)}</strong>{a.subject && <> of <code>{a.subject}</code></>}
        {": "}<span className="assertion-value">{fmtValue(a.value)}</span>
      </div>
      <div className="chip-row small">
        <StatusBadge status={r.status} label={r.label} /> <OriginTag origin={a.origin} />
        {a.confidence != null && <span className="muted">confidence {Math.round(a.confidence * 100)}%</span>}
        <span className="muted">v{a.version}{a.updated_at ? ` · ${fmtDate(a.updated_at)}` : ""}</span>
      </div>
      {a.note && <p className="small">{a.note}</p>}
      {a.evidence.length > 0 && (
        <ul className="evidence-list small" aria-label={`Evidence for ${name}`}>
          {a.evidence.map((e, k) => <li key={`${e.kind}-${e.ref}-${k}`}><Tag>{e.kind}</Tag> <code>{e.ref}</code></li>)}
        </ul>
      )}
      {canReview && a.review_state === "suggested" && (
        <div className="chip-row">
          <button type="button" className="btn btn-xs btn-primary" onClick={onReview} disabled={busy} aria-label={`Confirm ${name}`}>Confirm</button>
          <button type="button" className="btn btn-xs btn-ghost" onClick={onReject} disabled={busy} aria-label={`Reject ${name}`}>Reject</button>
        </div>
      )}
    </li>
  );
}

function AddAssertionForm({ onAdd, busy }: { onAdd: (group: string, field: string, subject: string, value: string, note: string) => void; busy: boolean }) {
  const id = useId();
  const [group, setGroup] = useState("domain");
  const [field, setField] = useState("alias");
  const [subject, setSubject] = useState("");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const fields = BRIEF_GROUPS.find((g) => g.id === group)?.fields ?? {};
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    onAdd(group, field, subject.trim(), value.trim(), note.trim());
  };
  return (
    <form className="form" onSubmit={submit} aria-label="State a fact">
      <div className="form-row">
        <Field label="Group" htmlFor={`${id}-group`}>
          <select id={`${id}-group`} value={group} onChange={(e) => {
            setGroup(e.target.value);
            setField(Object.keys(BRIEF_GROUPS.find((g) => g.id === e.target.value)?.fields ?? {})[0] ?? "");
          }}>
            {BRIEF_GROUPS.map((g) => <option key={g.id} value={g.id}>{g.label}</option>)}
          </select>
        </Field>
        <Field label="Field" htmlFor={`${id}-field`}>
          <select id={`${id}-field`} value={field} onChange={(e) => setField(e.target.value)}>
            {Object.entries(fields).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </Field>
      </div>
      <div className="form-row">
        <Field label="About" htmlFor={`${id}-subject`} hint="A table (schema.table) or column (schema.table.column); optional.">
          <input id={`${id}-subject`} value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="stg_sn.incident.priority" />
        </Field>
        <Field label="Value" htmlFor={`${id}-value`}>
          <input id={`${id}-value`} value={value} onChange={(e) => setValue(e.target.value)} placeholder="P1 = critical" />
        </Field>
      </div>
      <Field label="Note" htmlFor={`${id}-note`}>
        <input id={`${id}-note`} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={busy || !value.trim()}>Save as a fact</button>
      </div>
    </form>
  );
}

/**
 * Data → Brief & readiness (P4-04): the workspace brief as facts (reviewed or validated) and open
 * questions (suggestions, with their origin and evidence). An editor confirms or rejects a question,
 * or states a fact; each change is a new brief version made against the version on screen, so a
 * concurrent edit is shown as "someone changed this" instead of being overwritten.
 */
export function BriefPanel({ wsId, role }: { wsId: string; role: string | undefined }) {
  const brief = useAsync(() => api.brief(wsId), [wsId]);
  const versions = useAsync(() => api.briefVersions(wsId), [wsId]);
  const act = useAction();
  const [lastEdit, setLastEdit] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const canEdit = roleAtLeast(role, "editor");
  const canSuggest = roleAtLeast(role, "analyst");
  const b = brief.data;

  const apply = async (ops: Parameters<typeof api.patchBrief>[2]["ops"], what: string) => {
    if (!b) return;
    setNotice(null);
    setLastEdit({ version: b.version, assertions: b.assertions, ops });
    const r = await act.run(() => api.patchBrief(wsId, b.version, { ops }));
    if (r) {
      brief.setData(r);
      void versions.reload();
      const outdated = r.impact?.readiness_assessments_outdated;
      setNotice(`${what} Brief version ${r.version}.${outdated ? ` ${outdated} readiness assessment${outdated > 1 ? "s are" : " is"} now out of date.` : ""}`);
    }
  };
  const refresh = async () => {
    setNotice(null);
    const r = await act.run(() => api.refreshBrief(wsId));
    if (r) {
      brief.setData(r);
      void versions.reload();
      setNotice(`Suggestions refreshed: ${r.added?.length ?? 0} added, ${r.updated?.length ?? 0} updated. Your decisions were kept.`);
    }
  };

  if (brief.error) return <ErrorBox error={brief.error} onRetry={brief.reload} />;
  if (!b) return <Loading />;
  const live = b.assertions.filter((a) => a.review_state !== "rejected");
  const facts = live.filter(isFact);
  const questions = live.filter((a) => a.review_state === "suggested");
  const rejected = b.assertions.filter((a) => a.review_state === "rejected");
  const byGroup = (list: Assertion[]) => BRIEF_GROUPS.map((g) => ({ g, items: list.filter((a) => a.group === g.id) })).filter((x) => x.items.length);
  const item = (a: Assertion) => (
    <AssertionItem key={a.key} a={a} canReview={canEdit} busy={act.busy}
      onReview={() => void apply([{ op: "review", key: a.key }], "Confirmed.")}
      onReject={() => void apply([{ op: "reject", key: a.key }], "Rejected; it stays visible as rejected.")} />
  );

  return (
    <div className="stack">
      <div className="toolbar">
        <p className="muted small">What the data means for this workspace. Only facts are used by readiness and planning; open questions are
          suggestions until a person or a deterministic check confirms them. Version {b.version || "none yet"}.</p>
        {canSuggest && <button type="button" className="btn btn-sm" onClick={() => void refresh()} disabled={act.busy}>Refresh suggestions</button>}
      </div>
      {isStaleEdit(act.failure)
        ? <StaleEditNotice what="The brief" mine={lastEdit} loadCurrent={() => api.brief(wsId)}
          onReload={() => { act.clear(); void brief.reload(); void versions.reload(); }} />
        : <ErrorBox error={act.error} />}
      {notice && <Notice tone="success">{notice}</Notice>}
      {b.assertions.length === 0 && (
        <EmptyState title="No brief yet">{canSuggest ? "Refresh suggestions to draft one from the catalog, or state a fact below." : "An analyst drafts one from the catalog."}</EmptyState>
      )}
      <div className="grid-2">
        <Card title={`Open questions (${questions.length})`} label={`Open questions (${questions.length})`}>
          {questions.length === 0 ? <p className="muted small">No open questions.</p> : byGroup(questions).map(({ g, items }) => (
            <div key={g.id} role="group" aria-label={`Questions: ${g.label}`}>
              <h3 className="small">{g.label}</h3>
              <ul className="assertion-list">{items.map(item)}</ul>
            </div>
          ))}
        </Card>
        <Card title={`Facts (${facts.length})`} label={`Facts (${facts.length})`}>
          {facts.length === 0 ? <p className="muted small">No confirmed facts yet.</p> : byGroup(facts).map(({ g, items }) => (
            <div key={g.id} role="group" aria-label={`Facts: ${g.label}`}>
              <h3 className="small">{g.label}</h3>
              <ul className="assertion-list">{items.map(item)}</ul>
            </div>
          ))}
        </Card>
      </div>
      {canEdit && (
        <details className="card">
          <summary>State a fact (glossary alias, grain, unit…)</summary>
          <div className="card-body">
            <AddAssertionForm busy={act.busy} onAdd={(group, field, subject, value, note) => void apply([{
              op: "set", assertion: { group: group as never, field, subject: subject || null, value, note: note || null, evidence: [] } }], "Saved.")} />
          </div>
        </details>
      )}
      {rejected.length > 0 && (
        <details className="card">
          <summary>Rejected ({rejected.length})</summary>
          <div className="card-body"><ul className="assertion-list">{rejected.map(item)}</ul></div>
        </details>
      )}
      <details className="card">
        <summary>Versions{versions.data ? ` (${versions.data.length})` : ""}</summary>
        <div className="card-body">
          <ErrorBox error={versions.error} />
          <ol className="list small" aria-label="Brief versions">
            {(versions.data ?? []).map((v) => (
              <li key={v.version}>v{v.version} · {fmtDate(v.created_at)} · {v.created_by ?? "system"}{v.reason ? ` — ${v.reason}` : ""}</li>
            ))}
          </ol>
        </div>
      </details>
    </div>
  );
}

/** The brief at a glance for onboarding: read-only, with a link to its one home in Data. */
export function BriefSummary({ wsId, brief }: { wsId: string; brief: WorkspaceBrief | undefined }) {
  if (!brief) return null;
  const live = brief.assertions.filter((a) => a.review_state !== "rejected");
  const questions = live.filter((a) => a.review_state === "suggested");
  const facts = live.filter(isFact);
  return (
    <div className="brief-summary small">
      <p>{facts.length} fact{facts.length === 1 ? "" : "s"} confirmed, {questions.length} open question{questions.length === 1 ? "" : "s"}.</p>
      {questions.length > 0 && (
        <ul aria-label="Open questions in the brief">
          {questions.slice(0, 3).map((a) => (
            <li key={a.key}>{fieldLabel(a.group, a.field)}{a.subject ? ` of ${a.subject}` : ""}: {fmtValue(a.value)} <OriginTag origin={a.origin} /></li>
          ))}
        </ul>
      )}
      <Link className="btn btn-primary btn-sm" to={to.data(wsId, "brief")}>Review the brief</Link>
    </div>
  );
}
