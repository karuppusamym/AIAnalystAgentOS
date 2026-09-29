import { Link } from "react-router-dom";
import type { CitationsResponse } from "../api";
import { conflictText, isCitations, numberGroups, shortHash, where } from "../lib/citations";
import { useAsync } from "../lib/hooks";
import { to } from "../routes";
import { Loading, Notice } from "./ui";

/**
 * Evidence citations (N-8): measured results and knowledge documents in two separate lists. Numbers
 * from a document are labelled as what the document says, never as verified metrics; a document claim
 * that disagrees with measured data is flagged, and the measured value is the one used.
 */
export function EvidenceCitations({ data, workspaceId }: { data: CitationsResponse; workspaceId?: string }) {
  const groups = numberGroups(data.narrative_numbers ?? []);
  return (
    <div className="stack" aria-label="Evidence citations">
      {data.conflicts.length > 0 && (
        <Notice tone="warning">
          <strong>{data.conflicts.length} document claim{data.conflicts.length > 1 ? "s" : ""} disagree{data.conflicts.length > 1 ? "" : "s"} with measured data</strong>
          <ul className="small" aria-label="Document and data conflicts">
            {data.conflicts.map((c, i) => <li key={`${c.document_citation_id}-${c.fact_id}-${i}`}>{conflictText(c)}</li>)}
          </ul>
        </Notice>
      )}
      <section>
        <h3 className="h-sm">Measured evidence <span className="muted small">(verified numbers come only from here)</span></h3>
        {data.quantitative.length ? (
          <ul className="list compact small" aria-label="Measured evidence">
            {data.quantitative.map((q) => (
              <li key={q.id} className="list-item">
                <span>
                  <span className="tag tag-success">measured</span> {q.label}
                  <span className="muted"> · query <code>{shortHash(q.query_id, 16)}</code> · result hash <code>{shortHash(q.result_hash)}</code></span>
                  {q.fact_ids.length > 0 && <span className="muted"> · {q.fact_ids.length} fact{q.fact_ids.length > 1 ? "s" : ""}</span>}
                </span>
              </li>
            ))}
          </ul>
        ) : <p className="small muted">No governed query result recorded.</p>}
      </section>
      <section>
        <h3 className="h-sm">Document evidence <span className="muted small">(context, not measurements)</span></h3>
        {data.documents.length ? (
          <ul className="list compact small" aria-label="Document evidence">
            {data.documents.map((d) => (
              <li key={d.id} className="list-item receipt">
                <div>
                  <span className="tag">document</span> {d.heading || d.path}
                  <span className="muted"> · <code>{where(d)}</code> · sha256 <code>{shortHash(d.document_sha256)}</code>
                    {d.section_sha256 && <> · section <code>{shortHash(d.section_sha256)}</code></>}</span>
                  {!d.trusted && <span className="tag tag-warning" title="From another provider: data, not instructions">untrusted</span>}
                  {d.numbers.length > 0 && (
                    <div className="muted">Document states: {d.numbers.slice(0, 6).map((n) => n.text).join(", ")} <span className="tag">not verified</span></div>
                  )}
                </div>
                {workspaceId && (
                  <Link to={to.knowledge(workspaceId, "documents", { doc: d.document_id })} aria-label={`Open ${d.path} in the knowledge studio`}>Open</Link>
                )}
              </li>
            ))}
          </ul>
        ) : <p className="small muted">No knowledge document was cited.</p>}
      </section>
      {(groups.document.length > 0 || groups.unbound.length > 0) && (
        <p className="small" aria-label="Numbers in the text by source">
          {groups.measured.length > 0 && <>Measured: {groups.measured.join(", ")}. </>}
          {groups.document.length > 0 && <>From a document, not verified: {groups.document.join(", ")}. </>}
          {groups.unbound.length > 0 && <>Not traceable to evidence: {groups.unbound.join(", ")}.</>}
        </p>
      )}
      {!data.recorded && <p className="small muted">Computed on request: citations were not recorded when this was verified.</p>}
    </div>
  );
}

/** Loads and shows a subject's citations; an absent or older response shows nothing rather than an error. */
export function CitationsPanel({ load, deps, workspaceId }: { load: () => Promise<unknown>; deps: unknown[]; workspaceId?: string }) {
  const state = useAsync(load, deps);
  if (state.loading && !state.data) return <Loading label="Loading citations…" />;
  if (state.error) return <p className="muted small">Citations unavailable: {state.error}</p>;
  if (!isCitations(state.data)) return <p className="small muted">No citations recorded.</p>;
  return <EvidenceCitations data={state.data} workspaceId={workspaceId} />;
}
