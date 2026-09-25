import { useMemo } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, type Artifact, type ArtifactDetail } from "../api";
import { ChartView } from "../components/Chart";
import { DashboardPreview } from "../components/DashboardPreview";
import { LineageGraph } from "../components/LineageGraph";
import { Markdown } from "../components/Markdown";
import { Card, CodeBlock, EmptyState, ErrorBox, JsonView, KeyValue, Loading, PageHeader, RecordTable, StatusBadge } from "../components/ui";
import { fmtDate, fmtNumber, fmtPct, shortHash } from "../lib/format";
import { useAsync } from "../lib/hooks";
import type { Preview } from "../lib/charts";

const TYPE_ORDER = ["dashboard", "chart", "metric", "dataset", "narrative", "profile", "quality_report", "relationship_map", "query",
  "context_package", "plan", "report"];

export function StudioPage() {
  const { wsId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const selected = params.get("artifact");
  const typeFilter = params.get("type") ?? "";
  const list = useAsync(() => api.listArtifacts(wsId), [wsId]);

  const byType = useMemo(() => {
    const m = new Map<string, Artifact[]>();
    for (const a of list.data ?? []) {
      if (!m.has(a.type)) m.set(a.type, []);
      m.get(a.type)!.push(a);
    }
    return [...m.entries()].sort((a, b) => {
      const ia = TYPE_ORDER.indexOf(a[0]);
      const ib = TYPE_ORDER.indexOf(b[0]);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a[0].localeCompare(b[0]);
    });
  }, [list.data]);

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    setParams(next);
  };

  return (
    <div className="page">
      <PageHeader title="Studio" subtitle="Datasets, metrics, charts and dashboards produced by runs — versioned, with lineage." />
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data?.length === 0 && <EmptyState title="No artifacts yet">Runs create profiles, datasets, metrics, charts and dashboards.</EmptyState>}
      {!!list.data?.length && (
        <>
          <div className="chip-row type-filter" role="toolbar" aria-label="Filter by type">
            <button type="button" className={`chip ${typeFilter === "" ? "active" : ""}`} aria-pressed={typeFilter === ""} onClick={() => set({ type: null })}>
              all ({list.data.length})</button>
            {byType.map(([t, arr]) => (
              <button type="button" key={t} className={`chip ${typeFilter === t ? "active" : ""}`} aria-pressed={typeFilter === t} onClick={() => set({ type: t })}>
                {t} ({arr.length})</button>
            ))}
          </div>
          <div className="split">
            <div className="split-list">
              {byType.filter(([t]) => !typeFilter || t === typeFilter).map(([t, arr]) => (
                <section key={t} className="artifact-group">
                  <h2 className="group-title">{t.replace(/_/g, " ")}</h2>
                  <ul className="list selectable">
                    {arr.map((a) => (
                      <li key={a.id}>
                        <button type="button" className={`list-button ${a.id === selected ? "active" : ""}`} onClick={() => set({ artifact: a.id })}>
                          <span className="list-button-head"><span className="clamp-1">{a.name}</span><span className="muted small">v{a.version}</span></span>
                          <span className="chip-row"><StatusBadge status={a.status} />{a.platform && <span className="tag">{a.platform}</span>}
                            <span className="muted small">{fmtDate(a.created_at)}</span></span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
            <div className="split-detail">
              {selected ? <ArtifactView id={selected} wsId={wsId} onSelect={(id) => set({ artifact: id })} />
                : <EmptyState title="Select an artifact">Dashboards render as a live preview from their charts' data.</EmptyState>}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function ArtifactView({ id, wsId, onSelect }: { id: string; wsId: string; onSelect: (id: string) => void }) {
  const d = useAsync(() => api.getArtifact(id), [id]);
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />;
  if (!d.data) return <Loading />;
  const a = d.data;
  return (
    <div className="stack">
      <Card title={<>{a.name} <span className="muted small">· {a.type} · v{a.version}</span></>} actions={<>
        <StatusBadge status={a.status} />
        {a.external_url && <a className="btn btn-sm" href={a.external_url} target="_blank" rel="noreferrer noopener">Open in {a.platform ?? "BI"} ↗</a>}
      </>}>
        <KeyValue items={[
          ["Created by", a.creator_agent ? `agent:${a.creator_agent}` : a.creator_user ? `user:${a.creator_user}` : "—"],
          ["Run", a.run_id ?? "—"],
          ["Content hash", <code key="h" title={a.content_hash}>{shortHash(a.content_hash, 16)}</code>],
          ["Updated", fmtDate(a.updated_at)],
          ...(a.external_id ? [["External id", `${a.platform}:${a.external_id}`] as [string, string]] : []),
        ]} />
      </Card>
      <ArtifactContent a={a} wsId={wsId} />
      <Card title={`Versions (${a.versions.length})`}>
        <ul className="list compact">
          {a.versions.map((v) => (
            <li key={v.id} className="list-item">
              <span><strong>v{v.version}</strong> <code className="small">{shortHash(v.content_hash)}</code></span>
              <span className="muted small">{v.created_by ?? "—"} · {fmtDate(v.created_at)}</span>
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Lineage">
        <LineageGraph lineage={a.lineage} focus={{ type: a.type, id: a.id }}
          onSelect={(_, nodeId) => { if (nodeId.startsWith("art_")) onSelect(nodeId); }} />
      </Card>
    </div>
  );
}

function ArtifactContent({ a, wsId }: { a: ArtifactDetail; wsId: string }) {
  const c = a.content as Record<string, unknown>;
  switch (a.type) {
    case "chart":
      return (
        <Card title={String(c.title ?? a.name)} actions={<span className="tag">{String(c.chart_type)}</span>}>
          {c.description ? <p className="muted small">{String(c.description)}</p> : null}
          <ChartView type={String(c.chart_type)} preview={c.preview as Preview} title={String(c.title ?? "")} height={320} />
          <KeyValue items={[
            ["Intent", String(c.intent ?? "—")], ["Metric", String(c.metric ?? "—")], ["Dimension", String(c.dimension ?? "—")],
            ["Series", String(c.series ?? "—")], ["Insights", ((c.insight_codes as string[]) ?? []).join(", ") || "—"], ["Rationale", String(c.rationale ?? "—")],
          ]} />
        </Card>
      );
    case "dashboard":
      return <DashboardPreview wsId={wsId} artifact={a} />;
    case "dataset":
      return (
        <Card title="Dataset definition">
          {c.description ? <p>{String(c.description)}</p> : null}
          <CodeBlock code={String(c.sql ?? "")} />
          <KeyValue items={[["Time column", String(c.time_column ?? "—")], ["Rows", fmtNumber(c.row_count)],
            ["Source assets", ((c.source_assets as string[]) ?? []).join(", ") || "—"]]} />
          {Array.isArray(c.columns) && <RecordTable records={c.columns as Record<string, unknown>[]} maxRows={100} />}
        </Card>
      );
    case "metric":
      return (
        <Card title={String(c.display_name ?? c.name ?? a.name)} actions={<StatusBadge status={String(c.status ?? "")} />}>
          <p>{String(c.definition ?? "")}</p>
          <CodeBlock code={String(c.sql_expression ?? "")} label="SQL expression" />
          <KeyValue items={[["Format", String(c.format ?? "—")], ["Grain", String(c.grain ?? "—")],
            ["Dimensions", ((c.dimensions as string[]) ?? []).join(", ") || "—"], ["Filters", ((c.filters as string[]) ?? []).join(", ") || "—"]]} />
          {c.validation && Object.keys(c.validation as object).length ? <JsonView value={c.validation} collapsed label="Validation" /> : null}
        </Card>
      );
    case "narrative":
      return <Card title="Narrative"><Markdown text={String(c.markdown ?? "")} /></Card>;
    case "profile": {
      const cols = Array.isArray(c.columns) ? (c.columns as Record<string, unknown>[]) : [];
      return (
        <Card title={`Profile — ${fmtNumber(c.row_count)} rows`}>
          <RecordTable maxRows={200} records={cols.map((col) => ({
            column: col.name, type: col.data_type, semantic: col.semantic_type, null_rate: fmtPct(col.null_rate),
            distinct: col.distinct, min: col.min, max: col.max, mean: col.mean,
          }))} />
          <JsonView value={c} collapsed label="Full profile" />
        </Card>
      );
    }
    case "quality_report": {
      const issues = Array.isArray(c.issues) ? (c.issues as Record<string, unknown>[]) : [];
      return (
        <Card title={`Quality report — ${issues.length} issues`}>
          <RecordTable maxRows={200} records={issues.map((i) => ({ severity: i.severity, code: i.code, asset: i.asset, column: i.column, message: i.message }))} />
          <JsonView value={c} collapsed label="Full report" />
        </Card>
      );
    }
    default:
      return <Card title="Content"><JsonView value={c} /></Card>;
  }
}
