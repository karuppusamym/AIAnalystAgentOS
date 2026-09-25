import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { to } from "../routes";
import { api, type InsightDetail, type Verification } from "../api";
import { LineageGraph } from "../components/LineageGraph";
import { Card, CodeBlock, ConfidenceBar, EmptyState, ErrorBox, JsonView, KeyValue, Loading, PageHeader, PreviewTable, RecordTable, StatusBadge, Tag } from "../components/ui";
import { fmtDate, fmtMs, fmtNumber, fmtP, fmtPct, fmtValue, shortHash } from "../lib/format";
import { useAsync } from "../lib/hooks";

export function InsightsPage() {
  const { wsId = "", insightId } = useParams();
  const list = useAsync(() => api.listInsights(wsId), [wsId]);
  const [filter, setFilter] = useState<"all" | "verified" | "unverified">("all");
  const nav = useNavigate();
  const items = (list.data ?? []).filter((i) => filter === "all" || (filter === "verified" ? i.verified : !i.verified));

  return (
    <div className="page">
      <PageHeader title="Findings" subtitle="Every finding links to the queries, experiments and checks behind it." />
      <div className="split">
        <div className="split-list">
          <div className="seg" role="radiogroup" aria-label="Filter insights">
            {(["all", "verified", "unverified"] as const).map((f) => (
              <button key={f} type="button" role="radio" aria-checked={filter === f} className={`seg-btn ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>{f}</button>
            ))}
          </div>
          <ErrorBox error={list.error} onRetry={list.reload} />
          {list.loading && !list.data && <Loading />}
          {list.data && items.length === 0 && <EmptyState title="No insights">Insights appear when an analysis run tests hypotheses.</EmptyState>}
          <ul className="list selectable">
            {items.map((i) => (
              <li key={i.id}>
                <button type="button" className={`list-button ${i.id === insightId ? "active" : ""}`} onClick={() => nav(to.findings(wsId, i.id))}>
                  <span className="list-button-head">
                    <strong>{i.code}</strong> <span className="clamp-1">{i.title}</span>
                  </span>
                  <span className="chip-row">
                    <StatusBadge status={i.status} />
                    {i.verified && <span className="verified">✓ verified</span>}
                  </span>
                  <ConfidenceBar value={i.confidence} />
                </button>
              </li>
            ))}
          </ul>
        </div>
        <div className="split-detail">
          {insightId ? <InsightDetailView id={insightId} wsId={wsId} /> : <EmptyState title="Select an insight">Choose a finding to inspect its evidence.</EmptyState>}
        </div>
      </div>
    </div>
  );
}

function InsightDetailView({ id, wsId }: { id: string; wsId: string }) {
  const d = useAsync(() => api.getInsight(id), [id]);
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />;
  if (!d.data) return <Loading />;
  const i = d.data;
  const impact = Object.entries(i.business_impact ?? {});
  return (
    <div className="stack">
      <Card title={<><strong>{i.code}</strong> {i.title}</>} actions={<>
        <StatusBadge status={i.status} />
        {i.verified ? <span className="verified">✓ verified</span> : <Tag tone="warning">not verified</Tag>}
      </>}>
        <p className="finding">{i.finding}</p>
        <div className="grid-2">
          <div>
            <p className="small muted">Confidence</p>
            <ConfidenceBar value={i.confidence} />
          </div>
          <KeyValue items={[
            ["Population", fmtNumber(i.population_size)],
            ["Narrative", i.narrative_source],
            ["Run", <Link key="r" to={to.run(wsId, i.run_id)}><code>{i.run_id}</code></Link>],
            ["Created", fmtDate(i.created_at)],
          ]} />
        </div>
        {i.caveats.length > 0 && (
          <div className="caveats">
            <h3>Caveats</h3>
            <ul>{i.caveats.map((c, k) => <li key={k}>{c}</li>)}</ul>
          </div>
        )}
        {impact.length > 0 && (
          <div>
            <h3>Business impact</h3>
            <KeyValue items={impact.map(([k, v]) => [k.replace(/_/g, " "), fmtValue(v)])} />
          </div>
        )}
      </Card>
      <VerificationRecord v={i.verification} />
      <EvidenceSection d={i} />
      <Card title="Lineage">
        <LineageGraph lineage={i.lineage} focus={{ type: "insight", id: i.id }} />
      </Card>
    </div>
  );
}

function VerificationRecord({ v }: { v: Verification }) {
  if (!v || Object.keys(v).length === 0) return <Card title="REV verification"><EmptyState title="Not verified yet" /></Card>;
  const jev = v.verify?.jev;
  const im = v.verify?.independent_model;
  const second = v.verify?.second_method as Record<string, unknown> | null | undefined;
  return (
    <Card title="REV verification record" actions={<StatusBadge status={v.verified ? "verified" : "failed_verification"} />}>
      {v.reason && (
        <section>
          <h3>Reason</h3>
          <KeyValue items={[
            ["Claim", v.reason.claim],
            ["Question", v.reason.question],
            ["Required evidence", (v.reason.required_evidence ?? []).join(" · ")],
          ]} />
        </section>
      )}
      <section>
        <h3>Evaluate</h3>
        {v.evaluate?.length ? (
          <div className="table-wrap">
            <table className="table table-compact">
              <thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
              <tbody>
                {v.evaluate.map((c) => (
                  <tr key={c.check}>
                    <td><code>{c.check}</code></td>
                    <td><StatusBadge status={c.passed ? "ok" : "failed"} label={c.passed ? "pass" : "fail"} /></td>
                    <td className="small">{c.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="muted small">No checks recorded.</p>}
      </section>
      <section>
        <h3>Verify</h3>
        <KeyValue items={[
          ["Reproducible re-run", <StatusBadge key="r" status={v.verify?.reproducible ? "ok" : "failed"} label={v.verify?.reproducible ? "identical result hash" : "not reproduced"} />],
          ["Second method", second ? `${String(second.test ?? "")} · p=${fmtP(second.p_value)} · effect=${fmtNumber(second.effect_size as number, 3)}` : "—"],
          ["Independent model", im ? (im.unavailable ? `unavailable (${im.unavailable})` : `${im.model ?? ""}: ${im.review ? (im.review.supports ? "supports" : "does not support") : "—"}`) : "—"],
          ["JEV p_supports", jev ? <span key="j"><strong>{fmtPct(jev.p_supports)}</strong> <span className="muted small">({jev.model})</span></span> : "—"],
          ["Contradictions", (v.verify?.contradictions ?? []).join(", ") || "none"],
        ]} />
        {im?.review && <JsonView value={im.review} collapsed label="Independent model review" />}
      </section>
      {v.note && <p className="muted small">{v.note}</p>}
    </Card>
  );
}

function EvidenceSection({ d }: { d: InsightDetail }) {
  return (
    <Card title={`Evidence — ${d.queries.length} queries, ${d.experiments.length} experiments`}>
      {d.queries.length === 0 && d.experiments.length === 0 && <EmptyState title="No evidence recorded" />}
      {d.queries.map((q) => (
        <details key={q.id} className="evidence-item" open={d.queries.length <= 2}>
          <summary>
            <code>{q.id}</code> <StatusBadge status={q.status} /> <span className="muted small">{q.purpose} · {q.row_count} rows · {fmtMs(q.duration_ms)}
              {q.cache_hit ? " · cache hit" : ""} · result {shortHash(q.result_hash)}</span>
          </summary>
          <CodeBlock code={q.executed_sql ?? q.sql} label={q.executed_sql && q.executed_sql !== q.sql ? "Executed SQL (after gateway rewrite)" : "SQL"} />
          {q.rejected_reason && <p className="warn-text small">Rejected: {q.rejected_reason}</p>}
          <p className="muted small">Assets: {q.referenced_assets.join(", ") || "—"}{q.truncated ? " · truncated" : ""}</p>
          <PreviewTable columns={q.columns} preview={q.result_preview ?? []} maxRows={20} />
        </details>
      ))}
      {d.experiments.map((e) => {
        const res = e.result ?? {};
        const groups = (res.groups as Record<string, unknown>[] | undefined) ?? [];
        return (
          <details key={e.id} className="evidence-item">
            <summary>
              Experiment <code>{e.id}</code> <span className="tag">{e.role}</span> <span className="muted small">{e.method} · {String(res.test ?? "")} · p={fmtP(res.p_value)}
                · p_adj={fmtP(res.p_adjusted)} · {String(res.effect_label ?? "effect")}={fmtNumber(res.effect_size as number, 3)}</span>
            </summary>
            <KeyValue items={[["n", fmtNumber(res.n)], ["Supported", String(res.supported ?? "—")], ["Queries", e.query_ids.join(", ") || "—"]]} />
            {groups.length > 0 && <RecordTable records={groups} maxRows={30} />}
            <JsonView value={{ params: e.params, result: res }} collapsed label="Full experiment" />
          </details>
        );
      })}
    </Card>
  );
}
