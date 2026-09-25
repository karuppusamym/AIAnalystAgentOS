import { Link } from "react-router-dom";
import type { Hypothesis, Insight } from "../api";
import { fmtNumber, fmtP, fmtValue } from "../lib/format";
import { hypothesisIcon, toneFor } from "../lib/status";
import { buildInvestigationTree, orphanFindings, type HypNode } from "../lib/tree";
import { ConfidenceBar, EmptyState, RecordTable, StatusBadge, Tag } from "./ui";

function Finding({ wsId, insight }: { wsId: string; insight: Insight }) {
  return (
    <li className="tree-finding">
      <div className="tree-finding-head">
        <span className="tree-kind">Finding</span>
        <Link to={`/w/${wsId}/insights/${insight.id}`}><strong>{insight.code}</strong> {insight.title}</Link>
        <StatusBadge status={insight.status} />
        {insight.verified && <span className="verified" title="Passed REV verification">✓ verified</span>}
      </div>
      <p className="small">{insight.finding}</p>
      <div className="tree-finding-meta"><ConfidenceBar value={insight.confidence} /></div>
    </li>
  );
}

function HypothesisItem({ wsId, node }: { wsId: string; node: HypNode }) {
  const h = node.hypothesis;
  const r = h.result;
  const tone = toneFor(h.status);
  const highlights = r?.highlights ? Object.entries(r.highlights).filter(([, v]) => v !== null && v !== undefined && typeof v !== "object") : [];
  return (
    <li className="tree-hyp">
      <details open={h.status !== "superseded"}>
        <summary>
          <span className={`hyp-icon tone-${tone}`} aria-label={h.status} title={h.status}>{hypothesisIcon(h.status)}</span>
          <strong>{h.code}</strong>
          <span className="tree-statement">{h.statement}</span>
          <Tag tone={h.priority === "high" ? "danger" : h.priority === "medium" ? "warning" : "neutral"}>
            {h.priority} · {fmtNumber(h.priority_score, 2)}
          </Tag>
          <StatusBadge status={h.status} />
        </summary>
        <div className="tree-hyp-body">
          <div className="chip-row small">
            <span><span className="muted">Method:</span> {r?.test ?? (h.methods.join(", ") || "—")}</span>
            {r && <>
              <span><span className="muted">n=</span>{fmtNumber(r.n)}</span>
              <span><span className="muted">p=</span>{fmtP(r.p_value)}</span>
              <span><span className="muted">p_adj=</span>{fmtP(r.p_adjusted)}</span>
              <span><span className="muted">{r.effect_label ?? "effect"}=</span>{fmtNumber(r.effect_size, 3)}</span>
            </>}
            <span className="muted">origin: {h.origin} · iteration {h.iteration}</span>
          </div>
          {h.conclusion && <p className="small">{h.conclusion}</p>}
          {highlights.length > 0 && (
            <ul className="highlights small">
              {highlights.map(([k, v]) => <li key={k}><span className="muted">{k.replace(/_/g, " ")}:</span> {fmtValue(v)}</li>)}
            </ul>
          )}
          {r?.warnings?.length ? <p className="small warn-text">⚠ {r.warnings.join("; ")}</p> : null}
          {r?.groups?.length ? (
            <details className="groups">
              <summary className="small">Groups ({r.groups.length})</summary>
              <RecordTable records={r.groups} maxRows={30} />
            </details>
          ) : null}
          {node.findings.length > 0 && <ul className="tree-findings">{node.findings.map((i) => <Finding key={i.id} wsId={wsId} insight={i} />)}</ul>}
          {node.children.length > 0 && (
            <ul className="tree-children" aria-label={`Follow-ups of ${h.code}`}>
              {node.children.map((c) => <HypothesisItem key={c.hypothesis.id} wsId={wsId} node={c} />)}
            </ul>
          )}
        </div>
      </details>
    </li>
  );
}

export function InvestigationTree({ wsId, objective, hypotheses, insights }: { wsId: string; objective: string; hypotheses: Hypothesis[]; insights: Insight[] }) {
  const tree = buildInvestigationTree(objective, hypotheses, insights);
  const orphans = orphanFindings(hypotheses, insights);
  if (hypotheses.length === 0 && insights.length === 0) {
    return <EmptyState title="No hypotheses yet">The investigator proposes hypotheses after context, metadata and profiling.</EmptyState>;
  }
  return (
    <div className="tree">
      {tree.map((q) => (
        <div key={q.question} className="tree-question">
          <div className="tree-question-head"><span className="tree-kind">Question</span> {q.question}</div>
          <ul className="tree-hyps">
            {q.hypotheses.map((n) => <HypothesisItem key={n.hypothesis.id} wsId={wsId} node={n} />)}
          </ul>
        </div>
      ))}
      {orphans.length > 0 && (
        <div className="tree-question">
          <div className="tree-question-head"><span className="tree-kind">Other findings</span></div>
          <ul className="tree-findings">{orphans.map((i) => <Finding key={i.id} wsId={wsId} insight={i} />)}</ul>
        </div>
      )}
    </div>
  );
}
