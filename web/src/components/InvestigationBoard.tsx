import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Approval, type Hypothesis, type Insight } from "../api";
import { buildBoard, caveatsOf, modelOpinions, relevantApprovals, trustFacts, type TrustState } from "../lib/board";
import { fmtNumber, fmtP, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { toneFor } from "../lib/status";
import { to } from "../routes";
import { ConfidenceBar, EmptyState, ErrorBox, Loading, Notice, StateView, StatusBadge, Tag, TechnicalDetails, Value } from "./ui";

/**
 * The live hypothesis board (P4-U03, spec v3 §9 Investigate): one column per status, each
 * hypothesis card carrying its findings. A finding card opens "Why trust this" and records an
 * accept or reject signal for calibration. Raw JSON appears only under "Technical details".
 */
export function InvestigationBoard({ wsId, runId, hypotheses, insights, approvals, readOnly, onChanged }: {
  wsId: string; runId: string; hypotheses: Hypothesis[]; insights: Insight[]; approvals: Approval[]; readOnly: boolean; onChanged: () => void;
}) {
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [trustFor, setTrustFor] = useState<string | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const board = buildBoard(hypotheses, insights);
  if (hypotheses.length === 0 && insights.length === 0) {
    return <EmptyState title="No hypotheses yet">The investigator proposes hypotheses after context, metadata and profiling.</EmptyState>;
  }
  const hypById = new Map(hypotheses.map((h) => [h.id, h]));
  const open = insights.find((i) => i.id === trustFor) ?? null;
  const openTrust = (id: string, el: HTMLElement) => {
    opener.current = el;
    setTrustFor(id);
  };
  const cardProps = { wsId, runId, readOnly, onChanged, onTrust: openTrust };

  return (
    <div className="board-wrap">
      <div className="board" role="list" aria-label="Hypotheses by status">
        {board.columns.map((c) => (
          <div key={c.id} className={`board-col board-col-${c.id}`} role="listitem" aria-labelledby={`col-${c.id}`}>
            <header className="board-col-head">
              <h3 id={`col-${c.id}`}>{c.label} <span className="muted">({c.items.length})</span></h3>
              <p className="muted small">{c.hint}</p>
            </header>
            {c.items.length === 0 ? <p className="muted small board-empty">None</p> : (
              <ul className="board-cards">
                {c.items.map(({ hypothesis: h, findings }) => (
                  <li key={h.id}><HypothesisCard hypothesis={h} findings={findings} {...cardProps} /></li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
      {board.orphans.length > 0 && (
        <section className="board-orphans" aria-labelledby="orphans-h">
          <h3 id="orphans-h">Findings without a hypothesis</h3>
          <ul className="board-cards board-cards-row">
            {board.orphans.map((i) => <li key={i.id}><FindingCard insight={i} {...cardProps} /></li>)}
          </ul>
        </section>
      )}
      {board.superseded.length > 0 && (
        <div className="board-superseded">
          <button type="button" className="btn btn-xs btn-ghost" aria-expanded={showSuperseded} onClick={() => setShowSuperseded((v) => !v)}>
            {showSuperseded ? "Hide" : "Show"} superseded hypotheses ({board.superseded.length})
          </button>
          {showSuperseded && (
            <ul className="small">
              {board.superseded.map((h) => <li key={h.id}><strong>{h.code}</strong> {h.statement} <span className="muted">— replaced by a replan</span></li>)}
            </ul>
          )}
        </div>
      )}
      {open && (
        <TrustDrawer insight={open} hypothesis={open.hypothesis_id ? hypById.get(open.hypothesis_id) ?? null : null}
          approvals={approvals} wsId={wsId}
          onClose={() => {
            setTrustFor(null);
            opener.current?.focus();
          }} />
      )}
    </div>
  );
}

type CardProps = { wsId: string; runId: string; readOnly: boolean; onChanged: () => void; onTrust: (id: string, el: HTMLElement) => void };

function HypothesisCard({ hypothesis: h, findings, ...rest }: { hypothesis: Hypothesis; findings: Insight[] } & CardProps) {
  const r = h.result;
  return (
    <article className={`hyp-card tone-border-${toneFor(h.status)}`} aria-label={`Hypothesis ${h.code}`}>
      <div className="hyp-card-head">
        <strong>{h.code}</strong>
        <Tag tone={h.priority === "high" ? "danger" : h.priority === "medium" ? "warning" : "neutral"}>{h.priority}</Tag>
        <StatusBadge status={h.status} />
      </div>
      <p className="hyp-card-statement">{h.statement}</p>
      <p className="muted small">
        {r?.test ?? (h.methods.join(", ") || "method pending")}
        {r && <> · n <Value value={r.n} format="int" /> · q {r.p_adjusted === null || r.p_adjusted === undefined ? <Value value={null} /> : fmtP(r.p_adjusted)}
          {r.effect_size !== null && r.effect_size !== undefined && <> · {r.effect_label ?? "effect"} {fmtNumber(r.effect_size, 3)}</>}</>}
      </p>
      {h.conclusion && <p className="small">{h.conclusion}</p>}
      {findings.length > 0 && (
        <ul className="finding-list" aria-label={`Findings for ${h.code}`}>
          {findings.map((i) => <li key={i.id}><FindingCard insight={i} {...rest} /></li>)}
        </ul>
      )}
    </article>
  );
}

type Signal = { kind: "accepted" | "rejected"; note?: string } | { kind: "refused"; message: string };

function FindingCard({ insight: i, wsId, runId, readOnly, onChanged, onTrust }: { insight: Insight } & CardProps) {
  const act = useAction();
  const [signal, setSignal] = useState<Signal | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const rejected = i.status === "rejected";

  const accept = async () => {
    try {
      await api.findingOutcome(i.id, "accept");
      setSignal({ kind: "accepted" });
    } catch (err) {
      if (err instanceof ApiError && (err.status === 400 || err.status === 403 || err.status === 422)) {
        setSignal({ kind: "refused", message: err.message });
      } else {
        act.setError(err instanceof Error ? err.message : String(err));
      }
    }
  };
  const reject = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.feedback(wsId, runId, { text: reason.trim(), kind: "reject_finding", target_type: "insight", target_id: i.id }));
    if (r) {
      setSignal({ kind: "rejected", note: r.replan ? `Replanned to plan v${r.replan.plan_version}.` : undefined });
      setRejecting(false);
      setReason("");
      onChanged();
    }
  };

  return (
    <article className={`finding-card ${i.verified ? "finding-verified" : ""}`} aria-label={`Finding ${i.code}`}>
      <div className="finding-head">
        <span className="tree-kind">Finding</span>
        <Link to={to.findings(wsId, i.id)}><strong>{i.code}</strong> {i.title}</Link>
      </div>
      <div className="chip-row small">
        <StatusBadge status={i.status} label={i.verified ? "verified" : i.status.replace(/_/g, " ")} />
        <span className="muted">n</span> <Value value={i.population_size || null} format="int" />
      </div>
      <p className="small">{i.finding}</p>
      <ConfidenceBar value={i.confidence} />
      <div className="finding-actions">
        <button type="button" className="btn btn-xs" onClick={(e) => onTrust(i.id, e.currentTarget)}>Why trust this</button>
        {!readOnly && !rejected && signal?.kind !== "accepted" && (
          <button type="button" className="btn btn-xs btn-success" disabled={act.busy} onClick={() => void accept()}>Accept</button>
        )}
        {!readOnly && !rejected && !rejecting && (
          <button type="button" className="btn btn-xs btn-danger" disabled={act.busy} onClick={() => setRejecting(true)}>Reject</button>
        )}
      </div>
      {rejecting && (
        <form className="form finding-reject" onSubmit={reject}>
          <label htmlFor={reasonId} className="small">Why is {i.code} wrong or not useful?</label>
          <textarea id={reasonId} rows={2} value={reason} required onChange={(e) => setReason(e.target.value)}
            placeholder="The Network group was reorganised in August; the comparison is not like for like." />
          <p className="muted small">Rejecting marks the finding and its hypothesis rejected and replans the run. It is recorded as negative knowledge.</p>
          <div className="form-actions">
            <button type="button" className="btn btn-xs btn-ghost" onClick={() => setRejecting(false)}>Cancel</button>
            <button type="submit" className="btn btn-xs btn-danger" disabled={act.busy || !reason.trim()}>Reject finding</button>
          </div>
        </form>
      )}
      <ErrorBox error={act.error} />
      {signal?.kind === "accepted" && <p className="small success-text" role="status">Accepted — signal recorded for calibration.</p>}
      {signal?.kind === "rejected" && <p className="small" role="status">Rejected. {signal.note}</p>}
      {signal?.kind === "refused" && <StateView kind="refused" title="Accept not recorded">{signal.message}</StateView>}
    </article>
  );
}

const TRUST_ICON: Record<TrustState, string> = { pass: "✓", fail: "✗", unknown: "?", info: "•" };
const TRUST_TONE: Record<TrustState, string> = { pass: "success", fail: "danger", unknown: "neutral", info: "info" };

/** "Why trust this": a modal drawer with the finding's verification evidence, check by check. */
function TrustDrawer({ insight: i, hypothesis, approvals, wsId, onClose }: {
  insight: Insight; hypothesis: Hypothesis | null; approvals: Approval[]; wsId: string; onClose: () => void;
}) {
  const detail = useAsync(() => api.getInsight(i.id), [i.id]);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const f = panelRef.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), summary, [tabindex]:not([tabindex='-1'])");
      if (!f.length) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const facts = trustFacts(i, hypothesis, detail.data ?? null);
  const caveats = caveatsOf(i);
  const opinions = modelOpinions(i);
  const gates = relevantApprovals(approvals);
  const deterministicOk = i.verification?.verified ?? i.verified;

  return (
    <div className="drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panelRef}>
        <header className="drawer-head">
          <h2 id={titleId}>Why trust {i.code}?</h2>
          <button type="button" className="btn btn-sm btn-ghost" ref={closeRef} onClick={onClose}>Close</button>
        </header>
        <div className="drawer-body">
          <p className="small">{i.finding}</p>
          {deterministicOk
            ? <Notice tone="success">Verified: every deterministic check passed. Model opinions below only adjusted confidence.</Notice>
            : <Notice tone="warning">Not verified: at least one deterministic check failed or was not recorded. Confidence is capped.</Notice>}
          <h3>Evidence</h3>
          <ul className="trust-list">
            {facts.map((f) => (
              <li key={f.id} className={`trust-item trust-${f.state}`}>
                <span className={`badge badge-${TRUST_TONE[f.state]}`} role="img" aria-label={f.state === "pass" ? "passed" : f.state === "fail" ? "failed" : "not reported"}>
                  <span aria-hidden="true">{TRUST_ICON[f.state]}</span>
                </span>
                <div>
                  <div><strong>{f.label}</strong>: {f.value}</div>
                  {f.detail && <div className="muted small">{f.detail}</div>}
                </div>
              </li>
            ))}
          </ul>
          {detail.loading && !detail.data && <Loading label="Loading query evidence…" />}
          {detail.error && <p className="muted small">Query evidence unavailable: {detail.error}</p>}
          <h3>Caveats</h3>
          {caveats.length ? <ul className="small">{caveats.map((c) => <li key={c}>{c}</li>)}</ul> : <p className="muted small">No data-quality or population caveats recorded.</p>}
          <h3>Model opinions <span className="muted small">(adjust confidence only)</span></h3>
          {opinions.length ? (
            <ul className="small">{opinions.map((o) => <li key={o.label}><span className="tag tag-jev">{o.label}</span> {o.value}</li>)}</ul>
          ) : <p className="muted small">None recorded.</p>}
          <h3>Approvals on this run</h3>
          {gates.length ? (
            <ul className="small">
              {gates.map((a) => (
                <li key={a.id}><StatusBadge status={a.status} /> {a.action.replace(/_/g, " ")} · payload <code title={a.payload_hash}>{shortHash(a.payload_hash, 12)}</code> · policy v{a.policy_version}</li>
              ))}
            </ul>
          ) : <p className="muted small">Nothing from this run needed an approval.</p>}
          <p className="small"><Link to={to.findings(wsId, i.id)}>Open the full evidence (queries, lineage)</Link></p>
          <TechnicalDetails value={{ verification: i.verification, evidence: i.evidence }} />
        </div>
      </div>
    </div>
  );
}
