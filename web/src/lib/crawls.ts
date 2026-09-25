/** Metadata-crawl helpers (services/crawler.py views). Pure functions. */
import type { Crawl, CrawlChanges, CrawlInput, CrawlStats } from "../api";
import { splitList } from "./sourceKinds";

export const CRAWL_POLL_MS = 2000;

export const CRAWL_STAGES = ["discover", "diff", "apply", "semantics", "pii", "profile", "relationships", "glossary", "enrich", "publish"];

export const isCrawlRunning = (c: Pick<Crawl, "status"> | null | undefined): boolean => c?.status === "running";

export interface CrawlFormState {
  mode: "" | "full" | "incremental";
  include: string;
  exclude: string;
  profile: boolean;
  enrich: boolean;
}

export function emptyCrawlForm(): CrawlFormState {
  return { mode: "", include: "", exclude: "", profile: true, enrich: false };
}

/**
 * Only send what the person set: an empty mode uses the admin default, empty patterns fall back
 * to the source's configured include/exclude, and profile/enrich are sent only when changed.
 */
export function buildCrawlInput(f: CrawlFormState): CrawlInput {
  const body: CrawlInput = {};
  if (f.mode) body.mode = f.mode;
  const inc = splitList(f.include);
  const exc = splitList(f.exclude);
  if (inc.length) body.include = inc;
  if (exc.length) body.exclude = exc;
  if (!f.profile) body.profile = false;
  if (f.enrich) body.enrich = true;
  return body;
}

/** One line of headline counts, e.g. "12 discovered · 2 new · 1 changed · 1 deprecated". */
export function crawlStatsSummary(stats: CrawlStats | null | undefined): string {
  const s = stats ?? {};
  const parts: string[] = [];
  const add = (n: unknown, label: string, always = false) => {
    if (typeof n === "number" && (always || n > 0)) parts.push(`${n.toLocaleString()} ${label}`);
  };
  add(s.discovered, "discovered", true);
  add(s.new, "new");
  add(s.changed, "changed");
  add(s.missing, "missing");
  add(s.deprecated, "deprecated");
  add(s.renamed, "renamed?");
  add(s.profiled, "profiled");
  add(s.tokens_saved, "tokens saved");
  return parts.join(" · ");
}

export interface DriftCounts {
  new: number;
  changed: number;
  missing: number;
  deprecated: number;
  renames: number;
}

export function driftCounts(changes: CrawlChanges | null | undefined): DriftCounts {
  const c = changes ?? {};
  return {
    new: c.new?.length ?? 0, changed: c.changed?.length ?? 0, missing: c.missing?.length ?? 0,
    deprecated: c.deprecated?.length ?? 0, renames: c.rename_candidates?.length ?? 0,
  };
}

export function hasDrift(changes: CrawlChanges | null | undefined): boolean {
  const d = driftCounts(changes);
  return d.new + d.changed + d.missing + d.deprecated + d.renames > 0;
}

/** Position of the current stage in the pipeline, for a progress label ("stage 4 of 10: semantics"). */
export function stageProgress(stage: string | null | undefined): string {
  if (!stage) return "";
  if (stage === "done") return "done";
  const i = CRAWL_STAGES.indexOf(stage);
  return i < 0 ? stage : `stage ${i + 1} of ${CRAWL_STAGES.length}: ${stage}`;
}
