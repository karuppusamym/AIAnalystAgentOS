import { Link } from "react-router-dom";
import { roleAtLeast, to } from "../routes";
import { api, type InsightDetail, type Verification } from "../api";
import { CitationsPanel } from "../components/Citations";
import { LineageGraph } from "../components/LineageGraph";
import { Card, CodeBlock, ConfidenceBar, EmptyState, ErrorBox, KeyValue, Loading, Notice, PreviewTable, RecordTable, StatusBadge, Tag, TechnicalDetails } from "../components/ui";
import { ReverifyButton, VerificationBadge, voidCause, WhyNumberButton, WhyState } from "../components/WhyNumber";
import { fmtDate, fmtMs, fmtNumber, fmtP, fmtPct, fmtValue, plural, shortHash } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { methodLabel } from "../lib/methods";

/**
 * One finding in Outputs (the finding screen): the answer, how it was checked, its numbers with
 * "Why this number?", the evidence and lineage. The list lives in Outputs, filtered to findings.
 */
export function FindingDetail({ id, wsId }: { id: string; wsId: string }) {
  const d = useAsync(() => api.getInsight(id), [id]);
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />;
  if (!d.data) return <Loading />;
  const i = d.data;
  const impact = Object.entries(i.business_impact ?? {});
  return (
    <div className="stack">
      <Card title={<><strong>{i.code}</strong> {i.title}</>} actions={<>
        {i.verification_state ? <VerificationBadge state={i.verification_state} showCause={false} />
          : i.verified ? <span className="verified">✓ verified</span> : <Tag tone="warning">not verified</Tag>}
      </>}>
        {voidCause(i.verification_state) && (
          <Notice tone="warning">This finding is void: {voidCause(i.verification_state)}.{" "}
            {roleAtLeast(ws.data?.role, "analyst")
              ? <>Re-verify it: a replay run re-tests its frozen analysis on today&apos;s data. <ReverifyButton state={i.verification_state} /></>
              : "An analyst can re-verify it."}
          </Notice>)}
        <p className="finding">{i.finding}</p>
        <FindingNumbers insightId={i.id} />
        <div className="grid-2">
          <div>
            <p className="small muted">Confidence</p>
            <ConfidenceBar value={i.confidence} />
          </div>
          <KeyValue items={[
            ["Population", fmtNumber(i.population_size)],
            ["Investigation", <Link key="r" to={to.run(wsId, i.run_id)}>Open the investigation</Link>],
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
      <Card title="Citations">
        <CitationsPanel load={() => api.insightCitations(i.id)} deps={[i.id]} workspaceId={wsId} />
      </Card>
      <Card title="Lineage">
        <LineageGraph lineage={i.lineage} focus={{ type: "insight", id: i.id }} />
      </Card>
    </div>
  );
}

/** The verifier's check codes in plain words (the code stays in the cell's tooltip). */
const CHECK_LABELS: Record<string, string> = {
  method_fit: "Method fits the question", sample_size: "Enough data", significance_after_bh: "Significant after multiple-test correction",
  effect_size: "Effect is large enough to matter", representative_population: "Population is representative", no_overreach: "Wording does not overclaim",
  reproducible_rerun: "Re-run gives the same result", second_method: "A second method agrees", numbers_bound: "Numbers match the results",
};

function checkLabel(code: string): string {
  const plain = code.replace(/_/g, " ");
  return CHECK_LABELS[code] ?? plain.charAt(0).toUpperCase() + plain.slice(1);
}

function VerificationRecord({ v }: { v: Verification }) {
  if (!v || Object.keys(v).length === 0) return <Card title="How it was checked"><EmptyState title="Not verified yet" /></Card>;
  const jev = v.verify?.jev;
  const im = v.verify?.independent_model;
  const second = v.verify?.second_method as Record<string, unknown> | null | undefined;
  return (
    <Card title="How it was checked" actions={<StatusBadge status={v.verified ? "verified" : "failed_verification"} />}>
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
                    <td title={c.check}>{checkLabel(c.check)}</td>
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
          ["Contradictions", (v.verify?.contradictions ?? []).join(", ") || "none"],
        ]} />
        <TechnicalDetails value={im?.review ? { independent_model_review: im.review } : undefined}>
          {jev && <p className="small">Decision model (JEV) p_supports <strong>{fmtPct(jev.p_supports)}</strong> ({jev.model}); it may only adjust confidence.</p>}
        </TechnicalDetails>
      </section>
      {v.note && <p className="muted small">{v.note}</p>}
    </Card>
  );
}

function EvidenceSection({ d }: { d: InsightDetail }) {
  return (
    <Card title={`Evidence — ${plural(d.queries.length, "query", "queries")}, ${plural(d.experiments.length, "test")}`}>
      {d.queries.length === 0 && d.experiments.length === 0 && <EmptyState title="No evidence recorded" />}
      {d.queries.map((q) => (
        <details key={q.id} className="evidence-item" open={d.queries.length <= 2}>
          <summary>
            Query <StatusBadge status={q.status} /> <span className="muted small">{q.purpose} · {q.row_count} rows · {fmtMs(q.duration_ms)}
              {q.cache_hit ? " · cache hit" : ""}</span>
          </summary>
          <CodeBlock code={q.executed_sql ?? q.sql} label={q.executed_sql && q.executed_sql !== q.sql ? "Executed SQL (after gateway rewrite)" : "SQL"} />
          {q.rejected_reason && <p className="warn-text small">Rejected: {q.rejected_reason}</p>}
          <p className="muted small">Assets: {q.referenced_assets.join(", ") || "—"}{q.truncated ? " · truncated" : ""}</p>
          <PreviewTable columns={q.columns} preview={q.result_preview ?? []} maxRows={20} />
          <TechnicalDetails><p className="small">Query <code>{q.id}</code> · result hash <code>{shortHash(q.result_hash)}</code></p></TechnicalDetails>
        </details>
      ))}
      {d.experiments.map((e) => {
        const res = e.result ?? {};
        const groups = (res.groups as Record<string, unknown>[] | undefined) ?? [];
        return (
          <details key={e.id} className="evidence-item">
            <summary>
              Test <span className="tag">{e.role}</span> <span className="muted small">{methodLabel(e.method)} · {res.test ? methodLabel(String(res.test)) : ""} · p={fmtP(res.p_value)}
                · p_adj={fmtP(res.p_adjusted)} · {res.effect_label ? methodLabel(String(res.effect_label)) : "effect"}={fmtNumber(res.effect_size as number, 3)}</span>
            </summary>
            <KeyValue items={[["n", fmtNumber(res.n)], ["Supported", String(res.supported ?? "—")], ["Queries", e.query_ids.join(", ") || "—"]]} />
            {groups.length > 0 && <RecordTable records={groups} maxRows={30} />}
            <TechnicalDetails value={{ id: e.id, params: e.params, result: res }} />
          </details>
        );
      })}
    </Card>
  );
}

/** Every number in the finding, each with its state and a "Why this number?" drawer (P7-08). */
function FindingNumbers({ insightId }: { insightId: string }) {
  const why = useAsync(() => api.whyInsight(insightId), [insightId]);
  if (why.error) return <p className="muted small">Number trail unavailable: {why.error}</p>;
  if (!why.data?.numbers.length) return null;
  return (
    <ul className="chip-row why-numbers" aria-label="Numbers in this finding">
      {why.data.numbers.map((n, k) => (
        <li key={`${n.text}-${k}`} className="why-chip">
          <strong>{n.text}</strong> <WhyState state={n.state} /> <WhyNumberButton insightId={insightId} number={n.text} />
        </li>
      ))}
    </ul>
  );
}
