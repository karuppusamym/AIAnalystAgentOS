import { useState } from "react";
import { api, type ModelSuggestion as Suggestion, type ModelValidation, type SuggestedTable } from "../api";
import { fmtDate, fmtNumber, fmtPct } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast } from "../routes";
import { Card, EmptyState, ErrorBox, Loading, Notice, StatusBadge, Tag } from "./ui";

const KEY_EVIDENCE: Record<string, string> = {
  approved: "approved key", declared: "declared by the source", measured_unique: "measured unique", profile_unique: "unique in the profile",
  none: "no key found",
};

const CARDINALITY: Record<string, string> = {
  one_to_one: "one to one", many_to_one: "many to one", one_to_many: "one to many", many_to_many: "many to many",
};

const ROLE_ORDER: Record<string, number> = { fact: 0, event: 1, bridge: 2, dimension: 3, reference: 4 };

/**
 * Data → Definitions → Suggested model: what the catalog, keys and measured joins say the data model
 * is — facts and dimensions, one key and grain per table, joins with their cardinality, and candidate
 * metrics — with the gaps named. Built by rules on the server (no model). An editor can measure keys
 * and joins through the gateway, discover more joins, and propose it; approval stays with someone else.
 */
export function ModelSuggestion({ wsId, role }: { wsId: string; role: string | undefined }) {
  const s = useAsync(() => api.modelSuggestion(wsId), [wsId]);
  const act = useAction();
  const [measured, setMeasured] = useState<ModelValidation | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const canEdit = roleAtLeast(role, "editor");

  const run = async (fn: () => Promise<string>) => {
    setNotice(null);
    const text = await act.run(fn);
    if (text) {
      setNotice(text);
      await s.reload();
    }
  };

  const d = s.data && Array.isArray(s.data.tables) ? s.data : undefined;
  return (
    <Card title="Suggested data model">
      <p className="muted small">
        Built from the catalog, the columns' profiles and the joins measured on the data — by rules, with no model. Nothing here is used by
        agents until it is proposed and approved by someone other than the person who proposed it.
      </p>
      {canEdit && (
        <div className="btn-row">
          <button type="button" className="btn btn-sm" disabled={act.busy}
            onClick={() => void run(async () => {
              const found = await api.discoverRelationships(wsId);
              return `${found.length} join${found.length === 1 ? "" : "s"} measured and queued for review.`;
            })}>Discover joins</button>
          <button type="button" className="btn btn-sm" disabled={act.busy}
            onClick={() => void run(async () => {
              const v = await api.validateModelSuggestion(wsId);
              setMeasured(v);
              const bad = v.tables.filter((t) => !t.unique).length + v.joins.filter((j) => j.fans_out).length;
              return `Measured ${v.tables.length} key${v.tables.length === 1 ? "" : "s"} and ${v.joins.length} join${v.joins.length === 1 ? "" : "s"} in ${v.queries} queries${bad ? `: ${bad} problem${bad === 1 ? "" : "s"} found` : ": all hold"}.`;
            })}>Measure keys and joins</button>
          <button type="button" className="btn btn-sm btn-primary" disabled={act.busy || !d?.tables.length}
            onClick={() => void run(async () => {
              const r = await api.proposeModelSuggestion(wsId);
              return r.status === "unchanged" ? "The approved model already matches this suggestion."
                : `Proposed as model version ${r.model_version}; ${r.candidates_queued} join${r.candidates_queued === 1 ? "" : "s"} queued. An approver reviews it under Data model.`;
            })}>Propose for approval</button>
        </div>
      )}
      {act.busy && <Loading label="Working through the query gateway…" />}
      <ErrorBox error={act.error ?? s.error} onRetry={s.error ? s.reload : undefined} />
      {notice && <Notice tone="success">{notice}</Notice>}
      {s.loading && !d && <Loading />}
      {d && d.tables.length === 0 && <EmptyState title="Nothing to suggest yet">Select tables in Sources; each crawl profiles them.</EmptyState>}
      {d && d.tables.length > 0 && <SuggestionBody d={d} measured={measured} />}
    </Card>
  );
}

function SuggestionBody({ d, measured }: { d: Suggestion; measured: ModelValidation | null }) {
  const byId = new Map(d.tables.map((t) => [t.asset_id, t]));
  const name = (id: string) => { const t = byId.get(id); return t ? t.business_name || t.name : id; };
  const keyCheck = new Map((measured?.tables ?? []).map((t) => [t.asset_id, t]));
  const tables = [...d.tables].sort((a, b) => (ROLE_ORDER[a.role ?? ""] ?? 9) - (ROLE_ORDER[b.role ?? ""] ?? 9) || a.name.localeCompare(b.name));
  const sm = d.summary;
  return (
    <div className="stack model-suggestion">
      <p className="small">
        {sm.tables} table{sm.tables === 1 ? "" : "s"}: {sm.facts} fact{sm.facts === 1 ? "" : "s"}, {sm.dimensions} dimension{sm.dimensions === 1 ? "" : "s"} ·{" "}
        {sm.relationships_validated} confirmed join{sm.relationships_validated === 1 ? "" : "s"}, {sm.relationships_pending} waiting for review ·{" "}
        {sm.keys_measured} key{sm.keys_measured === 1 ? "" : "s"} measured · <span className="muted">as of {fmtDate(d.generated_at)}</span>
      </p>

      {d.issues.length > 0 && (
        <Notice tone="warning">
          <strong>{d.issues.length} thing{d.issues.length === 1 ? "" : "s"} to settle before the model is solid:</strong>
          <ul className="model-issues">{d.issues.slice(0, 12).map((i, k) => <li key={k}>{i.message}</li>)}</ul>
        </Notice>
      )}

      {d.star_schemas.length > 0 && (
        <section aria-label="Star schemas">
          <h3 className="h-sm">How the tables fit together</h3>
          <ul className="star-list">
            {d.star_schemas.map((st) => (
              <li key={st.fact} className="star">
                <span className="star-fact">{name(st.fact)}</span>
                <span className="star-arrow" aria-hidden="true">→</span>
                <span className="chip-row">{st.dimensions.length ? st.dimensions.map((dim) => <Tag key={dim}>{name(dim)}</Tag>)
                  : <span className="muted small">no dimension joined yet</span>}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-label="Tables">
        <h3 className="h-sm">Tables</h3>
        <div className="table-wrap">
          <table className="table table-compact">
            <thead><tr><th>Table</th><th>Kind</th><th>One row is</th><th>Key</th><th>Time</th><th>Measures</th><th>Open points</th></tr></thead>
            <tbody>{tables.map((t) => <TableRow key={t.asset_id} t={t} check={keyCheck.get(t.asset_id)} />)}</tbody>
          </table>
        </div>
      </section>

      <section aria-label="Joins">
        <h3 className="h-sm">Joins</h3>
        {d.relationships.length === 0 ? <p className="muted small">No joins found yet. Discover joins measures candidates on the data.</p> : (
          <div className="table-wrap">
            <table className="table table-compact">
              <thead><tr><th>From</th><th>To</th><th>Cardinality</th><th>Confidence</th><th>Status</th><th>Measured</th></tr></thead>
              <tbody>
                {d.relationships.map((r, k) => {
                  const j = measured?.joins.find((x) => x.from === r.from.fq && x.to === r.to.fq);
                  return (
                    <tr key={k}>
                      <td>{name(r.from.asset_id)} <code className="small">{r.from.columns.join(", ")}</code></td>
                      <td>{name(r.to.asset_id)} <code className="small">{r.to.columns.join(", ")}</code></td>
                      <td className="small">{CARDINALITY[r.cardinality] ?? r.cardinality}</td>
                      <td className="small num">{r.confidence == null ? "—" : fmtPct(r.confidence, 0)}</td>
                      <td><StatusBadge status={r.status === "validated" ? "verified" : r.status} label={r.status === "validated" ? "confirmed" : r.status === "pending" ? "waiting for review" : "suggested"} /></td>
                      <td className="small">{j ? (j.fans_out ? <span className="warn-text">duplicates rows ({fmtNumber(j.rows_before)} → {fmtNumber(j.rows_after)})</span> : "keeps row count") : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {d.metrics.length > 0 && (
        <section aria-label="Candidate metrics">
          <h3 className="h-sm">Candidate metrics</h3>
          <p className="muted small">Starting points only: define and approve them under Metrics before they are used.</p>
          <ul className="metric-candidates">
            {d.metrics.map((m) => (
              <li key={`${m.table_fq}:${m.name}`}><strong>{m.label}</strong> <code className="small">{m.expression}</code>
                <span className="muted small"> — {m.reason}</span></li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function TableRow({ t, check }: { t: SuggestedTable; check?: ModelValidation["tables"][number] }) {
  const pk = t.primary_key;
  return (
    <tr>
      <td title={t.fq}><strong>{t.business_name || t.name}</strong><div className="small"><code>{t.name}</code></div></td>
      <td className="small">{t.role ?? "—"}</td>
      <td className="small">{t.entity ?? t.grain ?? "—"}</td>
      <td className="small">
        {pk.columns.length ? <code>{pk.columns.join(", ")}</code> : <span className="muted">none</span>}
        <div className={pk.unique === false || check?.unique === false ? "warn-text" : "muted"}>
          {check ? (check.unique ? `unique over ${fmtNumber(check.rows)} rows` : `not unique: ${fmtNumber(check.distinct_keys)} of ${fmtNumber(check.rows)}`)
            : KEY_EVIDENCE[pk.evidence] ?? pk.evidence}
        </div>
      </td>
      <td className="small">{t.time_column ? <code>{t.time_column}</code> : <span className="muted">—</span>}</td>
      <td className="small">{t.measures.length ? t.measures.slice(0, 4).join(", ") + (t.measures.length > 4 ? ` +${t.measures.length - 4}` : "") : <span className="muted">—</span>}</td>
      <td className="small">{t.issues.length ? t.issues.map((i) => <div key={i.code} className="warn-text">{i.message}</div>) : <span className="muted">none</span>}</td>
    </tr>
  );
}
