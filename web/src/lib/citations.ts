/**
 * Evidence citations (N-8): measured results and documents are two kinds, shown apart. Pure helpers.
 *
 * Only a quantitative citation backs a verified number. A number found only in a document is shown as
 * "the document says", never as a metric; a document claim that disagrees with measured data is a
 * conflict, and measured data wins.
 */
import type { CitationsResponse, DocumentCitation, EvidenceConflict, NarrativeNumber } from "../api";
import { fmtNumber } from "./format";

/** A response that is not a citations document (an older server, an empty mock) reads as no citations. */
export function isCitations(v: unknown): v is CitationsResponse {
  return !!v && typeof v === "object" && !Array.isArray(v) && Array.isArray((v as CitationsResponse).quantitative)
    && Array.isArray((v as CitationsResponse).documents);
}

export const shortHash = (h: string | null | undefined, n = 12): string => (h ? String(h).slice(0, n) : "—");

export function where(d: Pick<DocumentCitation, "path" | "anchor">): string {
  return d.anchor ? `${d.path}#${d.anchor}` : d.path;
}

/** Numbers of the text grouped by source; only `measured` are verified. */
export function numberGroups(numbers: NarrativeNumber[]): { measured: string[]; document: string[]; unbound: string[] } {
  const out = { measured: [] as string[], document: [] as string[], unbound: [] as string[] };
  for (const n of numbers) (n.source === "quantitative" ? out.measured : n.source === "document" ? out.document : out.unbound).push(n.text);
  return out;
}

/** "The document states 90% for resolution rate (Network); measured 45%." */
export function conflictText(c: EvidenceConflict): string {
  const measured = c.unit === "fraction" ? `${fmtNumber(c.measured_value * 100, 1)}%` : fmtNumber(c.measured_value, 4);
  const about = c.subject ? `${c.metric} (${c.subject})` : c.metric;
  return `${where(c)} states ${c.document_text} for ${about}; measured ${measured}. The measured value is used.`;
}
