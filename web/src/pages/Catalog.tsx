import { Fragment, lazy, Suspense, useCallback, useEffect, useId, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { to } from "../routes";
import { api, type CatalogAsset, type CatalogColumn } from "../api";
import { KpiEditor } from "../components/KpiEditor";
import { ConfidenceBar, EmptyState, ErrorBox, Field, Loading, Notice, PageHeader, StatusBadge, Tag, Card, Tabs } from "../components/ui";
import { fmtDate, fmtNumber, fmtPct } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";

// The studio's other tabs load on demand (the force-graph renderer among them), keeping the main bundle small.
const KnowledgeDocs = lazy(() => import("../components/KnowledgeDocs").then((m) => ({ default: m.KnowledgeDocs })));
const ReviewQueue = lazy(() => import("../components/ReviewQueue").then((m) => ({ default: m.ReviewQueue })));
const SemanticGraph = lazy(() => import("../components/SemanticGraph").then((m) => ({ default: m.SemanticGraph })));
const KnowledgeTransfer = lazy(() => import("../components/KnowledgeTransfer").then((m) => ({ default: m.KnowledgeTransfer })));

/** Who wrote a description: the source system, a rule, a model, or a person (a person always wins). */
export const ORIGIN_LABELS: Record<string, { label: string; tone: string; title: string }> = {
  source: { label: "source", tone: "info", title: "Comment from the source system" },
  rule: { label: "rule", tone: "neutral", title: "Written by the deterministic catalog rules" },
  model: { label: "model", tone: "jev", title: "Filled in by a model where rules were not confident" },
  user: { label: "user", tone: "success", title: "Curated by a person — never overwritten by the crawler" },
};

export function OriginBadge({ origin }: { origin: string | null | undefined }) {
  if (!origin) return null;
  const o = ORIGIN_LABELS[origin] ?? { label: origin, tone: "neutral", title: origin };
  return <span className={`tag tag-${o.tone}`} title={o.title}>{o.label}</span>;
}

const EDITOR_ROLES = new Set(["editor", "owner"]);

type StudioTab = "catalog" | "documents" | "review" | "graph" | "metrics" | "transfer";
const TABS: { id: StudioTab; label: string }[] = [
  { id: "catalog", label: "Catalog" }, { id: "documents", label: "Documents" }, { id: "review", label: "Review queue" },
  { id: "graph", label: "Semantic graph" }, { id: "metrics", label: "Metrics" }, { id: "transfer", label: "Import & export" },
];

/**
 * Knowledge → Knowledge studio (P4-U04): one screen, six tabs, inside the 20-screen budget. The
 * crawled catalog; the OKF documents of every pack the workspace sees (the workspace pack editable);
 * the review queue of AI and learning-loop drafts; the semantic graph (governed solid, inferred
 * dashed); the Ossie metrics editor (the same KPI editor as Build); and bundle import/export.
 */
export function CatalogPage() {
  const { wsId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = (TABS.some((t) => t.id === params.get("tab")) ? params.get("tab") : "catalog") as StudioTab;
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const canEdit = EDITOR_ROLES.has(ws.data?.role ?? "");
  const set = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      return next;
    });
  }, [setParams]);
  const switchTab = (t: StudioTab) => setParams(t === "catalog" ? {} : { tab: t });

  return (
    <div className="page">
      <PageHeader title="Knowledge studio"
        subtitle={<>The catalog, the workspace&apos;s knowledge documents and their review, the semantic model and its metrics. Crawl sources on
          the <Link to={to.sources(wsId)}>Sources</Link> page.</>} />
      <Tabs value={tab} onChange={switchTab} tabs={TABS} />
      <div className="tab-panel">
        <Suspense fallback={<Loading />}>
          {tab === "catalog" && <CatalogTab wsId={wsId} canEdit={canEdit} />}
          {tab === "documents" && <KnowledgeDocs wsId={wsId} params={params} set={set} />}
          {tab === "review" && <ReviewQueue wsId={wsId} canDecide={canEdit}
            onOpenDocument={(path) => setParams({ tab: "documents", path })} />}
          {tab === "graph" && <SemanticGraph wsId={wsId} />}
          {tab === "metrics" && <KpiEditor wsId={wsId} selected={params.get("kpi")} onSelect={(k) => set({ kpi: k })} />}
          {tab === "transfer" && <KnowledgeTransfer wsId={wsId} canEdit={canEdit}
            onBrowse={(packId) => setParams({ tab: "documents", pack: packId })} />}
        </Suspense>
      </div>
    </div>
  );
}

function CatalogTab({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const [draftQ, setDraftQ] = useState("");
  const [q, setQ] = useState("");
  const [domain, setDomain] = useState("");
  const [role, setRole] = useState("");
  const [deprecated, setDeprecated] = useState(false);
  const list = useAsync(() => api.catalog(wsId, { q, domain, role, include_deprecated: deprecated }), [wsId, q, domain, role, deprecated]);
  // Option lists grow from everything seen so far, so a filter never hides its own alternatives.
  const [known, setKnown] = useState<{ domains: string[]; roles: string[] }>({ domains: [], roles: [] });
  useEffect(() => {
    if (!list.data) return;
    setKnown((k) => ({
      domains: [...new Set([...k.domains, ...list.data!.map((a) => a.domain).filter((x): x is string => !!x)])].sort(),
      roles: [...new Set([...k.roles, ...list.data!.map((a) => a.role).filter((x): x is string => !!x)])].sort(),
    }));
  }, [list.data]);
  const [open, setOpen] = useState<string | null>(null);

  const search = (e: FormEvent) => {
    e.preventDefault();
    setQ(draftQ.trim());
  };
  const onCurated = (a: Partial<CatalogAsset> & { id: string }) =>
    list.setData((prev) => prev?.map((x) => (x.id === a.id ? { ...x, ...a } : x)));

  return (
    <div className="stack">
      <p className="muted small">Every crawled table with its business meaning, role, grain and sensitive columns.</p>
      <Card>
        <form className="form-inline catalog-filters" onSubmit={search} role="search" aria-label="Search the catalog">
          <label className="inline-field">
            <span className="sr-only">Search</span>
            <input type="search" value={draftQ} onChange={(e) => setDraftQ(e.target.value)} placeholder="Search tables, columns, descriptions…"
              aria-label="Search tables, columns and descriptions" />
          </label>
          <button type="submit" className="btn btn-sm btn-primary">Search</button>
          <label className="inline-field small">Domain
            <select value={domain} onChange={(e) => setDomain(e.target.value)} aria-label="Domain">
              <option value="">All</option>
              {known.domains.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
          <label className="inline-field small">Role
            <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role">
              <option value="">All</option>
              {known.roles.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <label className="toggle small">
            <input type="checkbox" checked={deprecated} onChange={(e) => setDeprecated(e.target.checked)} /> Include deprecated
          </label>
        </form>
      </Card>
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data?.length === 0 && (
        <EmptyState title={q || domain || role ? "No tables match" : "The catalog is empty"}>
          {q || domain || role ? "Try a broader search." : "Crawl a source to populate it."}
        </EmptyState>
      )}
      {!!list.data?.length && (
        <Card title={`${list.data.length} table${list.data.length === 1 ? "" : "s"}`}>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Table</th><th>Role</th><th>Domain</th><th>Grain</th><th>Confidence</th><th>Description</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {list.data.map((a) => (
                  <Fragment key={a.id}>
                    <tr className={open === a.id ? "row-active" : undefined}>
                      <td><code>{a.fq}</code>{a.business_name && <div className="small">{a.business_name}</div>}
                        <div className="muted small">{fmtNumber(a.row_count)} rows · {a.columns.length} columns</div></td>
                      <td className="small">{a.role ?? <span className="muted">—</span>}</td>
                      <td className="small">{a.domain ?? <span className="muted">—</span>}</td>
                      <td className="small">{a.grain ?? <span className="muted">—</span>}</td>
                      <td>{a.confidence != null ? <ConfidenceBar value={a.confidence} /> : <span className="muted">—</span>}</td>
                      <td className="small"><span className="clamp-2">{a.description ?? <span className="muted">No description</span>}</span>
                        <OriginBadge origin={a.description ? a.description_origin : null} /></td>
                      <td>
                        <div className="chip-row">
                          {a.reviewed ? <Tag tone="success">reviewed</Tag> : <Tag>unreviewed</Tag>}
                          {a.lifecycle !== "active" && <StatusBadge status={a.lifecycle === "deprecated" ? "superseded" : a.lifecycle} label={a.lifecycle} />}
                          {a.selected && <Tag tone="info">selected</Tag>}
                        </div>
                      </td>
                      <td>
                        <button type="button" className="btn btn-xs btn-ghost" aria-expanded={open === a.id} aria-controls={`cat-${a.id}`}
                          onClick={() => setOpen((o) => (o === a.id ? null : a.id))}>
                          {open === a.id ? "Hide" : `Columns (${a.columns.length})`}
                        </button>
                      </td>
                    </tr>
                    {open === a.id && (
                      <tr className="row-detail" id={`cat-${a.id}`}>
                        <td colSpan={8}>
                          <AssetCuration asset={a} canEdit={canEdit} onCurated={onCurated} />
                          <CatalogColumns columns={a.columns} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

export function AssetCuration({ asset, canEdit, onCurated }: {
  asset: CatalogAsset; canEdit: boolean; onCurated: (a: Partial<CatalogAsset> & { id: string }) => void;
}) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [businessName, setBusinessName] = useState(asset.business_name ?? "");
  const [description, setDescription] = useState(asset.description ?? "");
  const [reviewed, setReviewed] = useState(asset.reviewed);
  const act = useAction();

  const begin = () => {
    setBusinessName(asset.business_name ?? "");
    setDescription(asset.description ?? "");
    setReviewed(asset.reviewed);
    setEditing(true);
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const body: { business_name?: string; description?: string; reviewed?: boolean } = {};
    if (businessName !== (asset.business_name ?? "")) body.business_name = businessName;
    if (description !== (asset.description ?? "")) body.description = description;
    if (reviewed !== asset.reviewed) body.reviewed = reviewed;
    if (!Object.keys(body).length) {
      setEditing(false);
      return;
    }
    const r = await act.run(() => api.curateAsset(asset.id, body));
    if (r) {
      onCurated({ id: asset.id, business_name: r.business_name, description: r.description, description_origin: r.description_origin, reviewed: r.reviewed });
      setEditing(false);
    }
  };
  const markReviewed = async () => {
    const r = await act.run(() => api.curateAsset(asset.id, { reviewed: !asset.reviewed }));
    if (r) onCurated({ id: asset.id, reviewed: r.reviewed });
  };

  return (
    <div className="stack curation">
      {asset.description && (
        <p className="small">{asset.description} <OriginBadge origin={asset.description_origin} /></p>
      )}
      <p className="muted small">Last crawled {fmtDate(asset.last_crawled_at)}.</p>
      {canEdit && !editing && (
        <div className="chip-row">
          <button type="button" className="btn btn-sm" onClick={begin}>Edit description</button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => void markReviewed()} disabled={act.busy}>
            {asset.reviewed ? "Mark unreviewed" : "Mark reviewed"}
          </button>
        </div>
      )}
      {!canEdit && <p className="muted small">Editors and owners can curate names and descriptions.</p>}
      {editing && (
        <form className="form" onSubmit={save} aria-label={`Curate ${asset.fq}`}>
          <Notice tone="info">A person's curation always wins: once you edit a description, crawler rules and models never overwrite it.</Notice>
          <Field label="Business name" htmlFor={`${id}-bn`}>
            <input id={`${id}-bn`} value={businessName} maxLength={200} onChange={(e) => setBusinessName(e.target.value)} />
          </Field>
          <Field label="Description" htmlFor={`${id}-desc`}>
            <textarea id={`${id}-desc`} rows={3} value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <label className="toggle small">
            <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} /> Reviewed
          </label>
          <div className="form-actions">
            <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Saving…" : "Save"}</button>
          </div>
        </form>
      )}
      <ErrorBox error={act.error} />
    </div>
  );
}

export function CatalogColumns({ columns }: { columns: CatalogColumn[] }) {
  if (!columns.length) return <p className="muted small">No columns recorded.</p>;
  return (
    <div className="table-wrap">
      <table className="table table-compact">
        <caption className="sr-only">Columns</caption>
        <thead><tr><th>Column</th><th>Type</th><th>Role</th><th>Unit</th><th>Tags</th><th>PII</th><th>Glossary</th><th>Description</th></tr></thead>
        <tbody>
          {columns.map((c) => (
            <tr key={c.name}>
              <td><code>{c.name}</code>{c.business_name && <div className="muted small">{c.business_name}</div>}</td>
              <td className="small">{c.data_type}</td>
              <td className="small">{c.role ?? <span className="muted">—</span>}</td>
              <td className="small">{c.unit ?? <span className="muted">—</span>}</td>
              <td>
                {c.tags.length ? c.tags.map((t) => <Tag key={t} tone={t === "pii" || t === "restricted" ? "danger" : "warning"}>{t}</Tag>) : <span className="muted">—</span>}
                {c.tags.length > 0 && <div className="muted small" title="Crawler tags only ever tighten; owner tags are never removed.">by {c.tags_origin}</div>}
              </td>
              <td className="small">
                {c.pii?.category ? (
                  <span title={(c.pii.reasons ?? []).join("; ")}>{c.pii.category} <span className="muted">({c.pii.sensitivity}, {fmtPct(c.pii.confidence, 0)})</span></span>
                ) : <span className="muted">—</span>}
              </td>
              <td className="small">
                {c.glossary ? (
                  <span className="tag tag-info" title={c.glossary.reason ?? `glossary term ${c.glossary.term_id}`}>
                    {c.glossary.term ?? c.glossary.term_id}
                  </span>
                ) : <span className="muted">—</span>}
              </td>
              <td className="small clamp-2">{c.description ?? <span className="muted">—</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
