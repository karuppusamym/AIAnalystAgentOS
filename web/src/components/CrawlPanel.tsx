import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { api, type Crawl, type Source } from "../api";
import { fmtDate, fmtPct, durationBetween } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import {
  CRAWL_POLL_MS, buildCrawlInput, crawlStatsSummary, driftCounts, emptyCrawlForm, hasDrift, isCrawlRunning, stageProgress,
  type CrawlFormState,
} from "../lib/crawls";
import { EmptyState, ErrorBox, Field, KeyValue, Notice, StatusBadge, Tag } from "./ui";

/**
 * Metadata crawls of one source: start a crawl (full / incremental, include/exclude), the crawl
 * history, and a detail view with the schema-drift changes and the stage log. A running crawl is
 * polled every `pollMs` until it finishes.
 */
export function CrawlPanel({ wsId, source, refreshKey = 0, onFinished, pollMs = CRAWL_POLL_MS }: {
  wsId: string; source: Source; refreshKey?: number; onFinished?: (c: Crawl) => void; pollMs?: number;
}) {
  const id = useId();
  const list = useAsync(() => api.listCrawls(wsId, source.id), [wsId, source.id, refreshKey]);
  const [selected, setSelected] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<CrawlFormState>(emptyCrawlForm);
  const act = useAction();
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  const crawls = list.data ?? [];
  const running = crawls.filter(isCrawlRunning).map((c) => c.id).join(",");

  useEffect(() => {
    if (!running) return;
    const ids = running.split(",");
    let alive = true;
    const timer = setInterval(() => {
      for (const cid of ids) {
        api.getCrawl(cid).then((c) => {
          if (!alive) return;
          list.setData((prev) => prev?.map((x) => (x.id === c.id ? c : x)));
          if (!isCrawlRunning(c)) finishedRef.current?.(c);
        }).catch(() => undefined); // transient poll failure: try again on the next tick
      }
    }, pollMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // list.setData is stable in intent; only restart polling when the set of running crawls changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, pollMs]);

  const start = async (e: FormEvent) => {
    e.preventDefault();
    const c = await act.run(() => api.startCrawl(wsId, source.id, buildCrawlInput(form)));
    if (c) {
      list.setData((prev) => [c, ...(prev ?? []).filter((x) => x.id !== c.id)]);
      setSelected(c.id);
      setShowForm(false);
    }
  };

  const detail = crawls.find((c) => c.id === selected) ?? null;
  const set = (patch: Partial<CrawlFormState>) => setForm((f) => ({ ...f, ...patch }));

  return (
    <div className="stack crawl-panel">
      <div className="toolbar">
        <strong className="small">Metadata crawls</strong>
        <span className="muted small">Names, types, keys and comments only — row values never leave the gateway.</span>
        <button type="button" className="btn btn-sm" aria-expanded={showForm} onClick={() => setShowForm((s) => !s)}
          disabled={!!running}>{running ? "Crawl running…" : showForm ? "Close crawl form" : "Crawl"}</button>
      </div>
      {showForm && (
        <form className="form" onSubmit={start} aria-label={`Crawl ${source.name}`}>
          <div className="form-row">
            <Field label="Mode" htmlFor={`${id}-mode`}
              hint="Full crawls deprecate tables that disappeared; incremental crawls never do.">
              <select id={`${id}-mode`} value={form.mode} onChange={(e) => set({ mode: e.target.value as CrawlFormState["mode"] })}>
                <option value="">Admin default</option>
                <option value="incremental">Incremental</option>
                <option value="full">Full</option>
              </select>
            </Field>
            <Field label="Include patterns" htmlFor={`${id}-inc`} hint="Comma separated globs on schema.table or table. Empty = source config.">
              <input id={`${id}-inc`} value={form.include} onChange={(e) => set({ include: e.target.value })} placeholder="sales.*, incident" />
            </Field>
            <Field label="Exclude patterns" htmlFor={`${id}-exc`}>
              <input id={`${id}-exc`} value={form.exclude} onChange={(e) => set({ exclude: e.target.value })} placeholder="*_tmp, audit.*" />
            </Field>
          </div>
          <div className="toggle-group">
            <label className="toggle small">
              <input type="checkbox" checked={form.profile} onChange={(e) => set({ profile: e.target.checked })} /> Profile selected, changed tables
            </label>
            <label className="toggle small">
              <input type="checkbox" checked={form.enrich} onChange={(e) => set({ enrich: e.target.checked })} /> Model enrichment (only if the admin enabled it)
            </label>
          </div>
          <div className="form-actions">
            <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy}>{act.busy ? "Starting…" : "Start crawl"}</button>
          </div>
        </form>
      )}
      <ErrorBox error={act.error ?? list.error} onRetry={list.error ? list.reload : undefined} />
      {list.data && crawls.length === 0 && <p className="muted small">No crawls yet.</p>}
      {crawls.length > 0 && (
        <div className="table-wrap">
          <table className="table table-compact">
            <caption className="sr-only">Crawl history of {source.name}</caption>
            <thead><tr><th>Status</th><th>Mode</th><th>Trigger</th><th>Started</th><th>Duration</th><th>Result</th><th /></tr></thead>
            <tbody>
              {crawls.map((c) => (
                <tr key={c.id} className={c.id === selected ? "row-active" : undefined}>
                  <td><StatusBadge status={c.status} />{isCrawlRunning(c) && <div className="muted small">{stageProgress(c.stage)}</div>}</td>
                  <td className="small">{c.mode}</td>
                  <td className="small">{c.trigger}</td>
                  <td className="small">{fmtDate(c.started_at)}</td>
                  <td className="small">{c.finished_at ? durationBetween(c.started_at, c.finished_at) : "—"}</td>
                  <td className="small">
                    {c.status === "failed" ? <span className="warn-text clamp-1">{c.error}</span> : crawlStatsSummary(c.stats) || <span className="muted">—</span>}
                  </td>
                  <td>
                    <button type="button" className="btn btn-xs btn-ghost" aria-expanded={c.id === selected}
                      onClick={() => setSelected((s) => (s === c.id ? null : c.id))}>
                      {c.id === selected ? "Hide" : "Details"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detail && <CrawlDetail crawl={detail} />}
    </div>
  );
}

export function CrawlDetail({ crawl: c }: { crawl: Crawl }) {
  const d = driftCounts(c.changes);
  const ch = c.changes ?? {};
  const opts = c.options ?? {};
  return (
    <section className="crawl-detail stack" aria-label={`Crawl ${c.id}`}>
      <KeyValue items={[
        ["Status", <span key="s"><StatusBadge status={c.status} /> {isCrawlRunning(c) ? stageProgress(c.stage) : ""}</span>],
        ["Mode", `${c.mode} · ${c.trigger} · by ${c.started_by ?? "—"}`],
        ["Scope", `include ${opts.include?.length ? opts.include.join(", ") : "all"} · exclude ${opts.exclude?.length ? opts.exclude.join(", ") : "none"}`],
        ["Counts", crawlStatsSummary(c.stats) || "—"],
      ]} />
      {c.error && <Notice tone="danger">{c.error}</Notice>}
      {c.status === "succeeded" && !hasDrift(c.changes) && <Notice tone="success">No schema drift: every table in scope matches the previous crawl.</Notice>}
      {hasDrift(c.changes) && (
        <div className="stack">
          <h3 className="group-title">Schema drift</h3>
          <div className="chip-row">
            <Tag tone="success">{d.new} new</Tag>
            <Tag tone="warning">{d.changed} changed</Tag>
            <Tag tone="neutral">{d.missing} missing</Tag>
            <Tag tone="danger">{d.deprecated} deprecated</Tag>
            <Tag tone="info">{d.renames} rename candidates</Tag>
          </div>
          {!!ch.new?.length && <DriftList title="New tables" items={ch.new} />}
          {!!ch.changed?.length && (
            <div className="table-wrap">
              <table className="table table-compact">
                <caption className="sr-only">Changed tables</caption>
                <thead><tr><th>Changed table</th><th>Added columns</th><th>Removed columns</th><th>Retyped</th><th>Attributes</th></tr></thead>
                <tbody>
                  {ch.changed.map((x) => (
                    <tr key={x.key}>
                      <td><code>{x.key}</code></td>
                      <td className="small">{x.added.length ? x.added.map((a) => <Tag key={a} tone="success">+{a}</Tag>) : "—"}</td>
                      <td className="small">{x.removed.length ? x.removed.map((a) => <Tag key={a} tone="danger">−{a}</Tag>) : "—"}</td>
                      <td className="small">{x.retyped.length ? x.retyped.map((r) => (
                        <div key={r.name}><code>{r.name}</code>: {r.previous_type} → {r.current_type}</div>
                      )) : "—"}</td>
                      <td className="small">{x.attributes_changed ? "keys / nullability changed" : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!!ch.missing?.length && <DriftList title="Missing (not seen in this crawl)" items={ch.missing} />}
          {!!ch.deprecated?.length && (
            <DriftList title="Deprecated (removed from every scope)" items={ch.deprecated}
              note="A full crawl deprecates tables that disappeared; agents can no longer query them." />
          )}
          {!!ch.rename_candidates?.length && (
            <div>
              <h4 className="small">Rename candidates</h4>
              <ul className="list compact">
                {ch.rename_candidates.map((r) => (
                  <li key={`${r.previous_key}->${r.current_key}`} className="list-item small">
                    <span><code>{r.previous_key}</code> → <code>{r.current_key}</code></span>
                    <span className="muted">{fmtPct(r.similarity, 0)} similar</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
      <details className="crawl-log" open={isCrawlRunning(c) || c.status === "failed"}>
        <summary className="small">Stage log ({c.log?.length ?? 0})</summary>
        {!c.log?.length ? <EmptyState title="No log entries yet" /> : (
          <ol className="timeline small">
            {c.log.map((l, i) => (
              <li key={i}><span className="muted">{fmtDate(l.at)}</span> <Tag tone="info">{l.stage}</Tag> {l.message}</li>
            ))}
          </ol>
        )}
      </details>
    </section>
  );
}

function DriftList({ title, items, note }: { title: string; items: string[]; note?: string }) {
  return (
    <div>
      <h4 className="small">{title} ({items.length})</h4>
      {note && <p className="muted small">{note}</p>}
      <div className="chip-row">{items.map((k) => <code key={k} className="tag">{k}</code>)}</div>
    </div>
  );
}
