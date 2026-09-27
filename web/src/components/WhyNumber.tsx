import { useRef, useState } from "react";
import { api, type VerificationState, type WhyLink, type WhyNumber } from "../api";
import { fmtDate } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { Drawer } from "./Drawer";
import { CodeBlock, EmptyState, ErrorBox, Loading, Notice, StatusBadge, TechnicalDetails } from "./ui";

/** A verification badge in plain words (ADR-0020); a void one always says why. */
const BADGE_LABEL: Record<string, string> = {
  verified: "verified", pending: "verification pending", void: "void", superseded: "superseded",
  legacy: "verified by an older checker", failed: "failed verification", unverified: "not verified",
};
const BADGE_STATUS: Record<string, string> = {
  verified: "verified", pending: "pending", void: "invalidated", superseded: "superseded", legacy: "acknowledged",
  failed: "failed_verification", unverified: "draft",
};

export function voidCause(v: VerificationState | null | undefined): string | null {
  if (!v || v.badge !== "void") return null;
  const kind = v.void?.kind ? `${v.void.kind}: ` : "";
  return `${kind}${v.void?.reason || "a dependency changed"}`;
}

export function VerificationBadge({ state, showCause = true }: { state: VerificationState | null | undefined; showCause?: boolean }) {
  if (!state) return null;
  const cause = voidCause(state);
  return (
    <span className="verification-badge" data-badge={state.badge}>
      <StatusBadge status={BADGE_STATUS[state.badge] ?? state.badge} label={BADGE_LABEL[state.badge] ?? state.badge} />
      {cause && showCause && <span className="small void-cause"> Why void: {cause}. Re-run the investigation to verify it again.</span>}
    </span>
  );
}

const STATE_WORDS: Record<string, string> = {
  ok: "holds", changed: "changed since", void: "no longer valid", failed: "failed", broken: "cannot be traced",
  unknown: "not recorded", not_applicable: "does not apply",
};
const STATE_STATUS: Record<string, string> = {
  ok: "ok", changed: "pending", void: "invalidated", failed: "failed", broken: "failed", unknown: "unknown", not_applicable: "skipped",
};
const LINK_TITLE: Record<string, string> = {
  fact: "The fact behind it", step: "The step that produced it", query_receipt: "The query that read the data",
  data_version: "The data it was read from", semantic_version: "The metric definition", verdict: "The verification",
};

export function WhyState({ state }: { state: string }) {
  return <StatusBadge status={STATE_STATUS[state] ?? state} label={STATE_WORDS[state] ?? state.replace(/_/g, " ")} />;
}

const str = (v: unknown) => (v === null || v === undefined || v === "" ? null : String(v));

/** One link in plain words; SQL, hashes and versions stay under Technical details. */
function LinkDetail({ link }: { link: WhyLink }) {
  const d = (link.detail ?? {}) as Record<string, unknown>;
  let text: string | null = null;
  if (link.link === "fact") {
    text = [str(d.subject), str(d.role)?.replace(/_/g, " "), d.value !== undefined ? `value ${String(d.value)}${d.unit ? ` ${String(d.unit)}` : ""}` : null]
      .filter(Boolean).join(" · ");
  } else if (link.link === "step") {
    text = [str(d.code) && `hypothesis ${String(d.code)}`, str(d.method)?.replace(/_/g, " ")].filter(Boolean).join(" · ");
  } else if (link.link === "data_version") {
    text = str(d.asset);
  } else if (link.link === "semantic_version") {
    const metrics = Array.isArray(d.metrics) ? d.metrics : [];
    text = metrics.length ? `${metrics.length} metric definition${metrics.length > 1 ? "s" : ""}` : null;
  }
  const queries = link.link === "query_receipt" && Array.isArray(d.queries) ? d.queries as Record<string, unknown>[] : [];
  return (
    <>
      {text && <div className="small">{text}</div>}
      {link.reason && <div className="small">{link.reason}</div>}
      {queries.map((q, k) => (
        <div key={String(q.query_id ?? k)} className="small">
          {q.rows !== undefined ? `${String(q.rows)} rows` : "query"} {q.state ? <WhyState state={String(q.state)} /> : null}
          {q.sql ? <details className="evidence-item"><summary>Show the SQL</summary><CodeBlock code={String(q.sql)} /></details> : null}
        </div>
      ))}
    </>
  );
}

function NumberTrail({ n }: { n: WhyNumber }) {
  return (
    <section className="why-number" aria-label={`Number ${n.text}`}>
      <h3><span className="why-value">{n.text}</span> <WhyState state={n.state} /></h3>
      <ol className="trust-list">
        {n.links.map((l) => (
          <li key={l.link} className="trust-item">
            <WhyState state={l.state} />
            <div>
              <strong>{LINK_TITLE[l.link] ?? l.link.replace(/_/g, " ")}</strong>
              <LinkDetail link={l} />
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * "Why this number?" (P7-08): each number of a finding traced through its fact, step, query, data
 * version, definition and verdict, each link with its current state. Nothing is dropped: a broken
 * or voided link shows with its reason.
 */
export function WhyDrawer({ insightId, number, onClose }: { insightId: string; number?: string; onClose: () => void }) {
  const why = useAsync(() => api.whyInsight(insightId, number ? { number } : {}), [insightId, number]);
  const d = why.data;
  return (
    <Drawer title={number ? `Why ${number}?` : "Why this number?"} onClose={onClose}>
      <ErrorBox error={why.error} onRetry={why.reload} />
      {!d && !why.error && <Loading />}
      {d && (
        <>
          <p className="small">{d.subject.code ? <strong>{d.subject.code} </strong> : null}{d.subject.finding ?? d.subject.title}</p>
          <p className="small">Verification: <VerificationBadge state={d.verification_state} /></p>
          {d.state === "ok"
            ? <Notice tone="success">Every link behind {d.numbers.length === 1 ? "this number" : "these numbers"} still holds.</Notice>
            : <Notice tone="warning">At least one link {STATE_WORDS[d.state] === "holds" ? "holds" : `is ${STATE_WORDS[d.state] ?? d.state}`}; see below.</Notice>}
          {d.numbers.length === 0 && <EmptyState title="No numbers in this finding" />}
          {d.numbers.map((n, k) => <NumberTrail key={`${n.text}-${k}`} n={n} />)}
          {d.verification_state?.created_at && <p className="muted small">Verified {fmtDate(d.verification_state.created_at)}.</p>}
          <TechnicalDetails value={d} />
        </>
      )}
    </Drawer>
  );
}

/** A number with its "Why this number?" button; the drawer returns focus here when it closes. */
export function WhyNumberButton({ insightId, number, label }: { insightId: string; number?: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button type="button" ref={ref} className="btn btn-xs btn-ghost why-btn" onClick={() => setOpen(true)} aria-haspopup="dialog">
        {label ?? "Why this number?"}{number && <span className="sr-only"> ({number})</span>}
      </button>
      {open && <WhyDrawer insightId={insightId} number={number} onClose={() => { setOpen(false); ref.current?.focus(); }} />}
    </>
  );
}
