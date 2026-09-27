import { useCallback, useMemo } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type Artifact, type ArtifactDetail, type Insight, type RecipeRun } from "../api";
import { ChartView } from "../components/Chart";
import { PublishCard } from "../components/DashboardPublish";
import { DashboardPreview } from "../components/DashboardPreview";
import { LineageGraph } from "../components/LineageGraph";
import { Markdown } from "../components/Markdown";
import { VerificationBadge, voidCause } from "../components/WhyNumber";
import { Card, CodeBlock, ConfidenceBar, EmptyState, ErrorBox, KeyValue, Loading, PageHeader, RecordTable, StatusBadge, TechnicalDetails } from "../components/ui";
import type { Preview } from "../lib/charts";
import { fmtDate, fmtNumber, fmtPct, shortHash } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { to, type OutputType } from "../routes";
import { FindingDetail } from "./Insights";
import { GenerateReportForm, ReportRow } from "./Reports";

/** Artifact type → output filter. Metrics are not outputs: their one home is Data (spec v4 §15). */
export function outputTypeOf(artifactType: string): OutputType | null {
  if (artifactType === "metric") return null;
  if (artifactType === "dashboard" || artifactType === "report" || artifactType === "dataset" || artifactType === "chart") return artifactType;
  return "other";
}

export const OUTPUT_FILTERS: { id: OutputType; label: string }[] = [
  { id: "finding", label: "Findings" }, { id: "dashboard", label: "Dashboards" }, { id: "report", label: "Reports" },
  { id: "dataset", label: "Datasets" }, { id: "chart", label: "Charts" }, { id: "prepared", label: "Prepared data" }, { id: "other", label: "Other" },
];

type Item =
  | { kind: "finding"; id: string; at: string; finding: Insight }
  | { kind: "artifact"; id: string; at: string; artifact: Artifact; type: OutputType }
  | { kind: "prepared"; id: string; at: string; run: RecipeRun };

const typeOf = (i: Item): OutputType => (i.kind === "artifact" ? i.type : i.kind);

/**
 * Outputs (spec v4 §15): one list of what work produced, filtered by type: findings, dashboards,
 * reports, datasets, charts and prepared data. Dashboards and reports are filters of this list, not
 * second lists; opening a dashboard shows its preview and the publication request.
 */
export function OutputsPage() {
  const { wsId = "", insightId } = useParams();
  const [params, setParams] = useSearchParams();
  const requested = params.get("type") as OutputType | null;
  const type: OutputType | "" = insightId ? "finding" : OUTPUT_FILTERS.some((f) => f.id === requested) ? requested! : "";
  const selected = insightId ?? params.get("artifact") ?? params.get("dashboard") ?? params.get("run");
  const artifacts = useAsync(() => api.listArtifacts(wsId), [wsId]);
  const findings = useAsync(() => api.listInsights(wsId), [wsId]);
  const prepared = useAsync(() => api.listRecipeRuns(wsId), [wsId]);
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

  const items = useMemo<Item[]>(() => {
    const out: Item[] = [];
    for (const f of findings.data ?? []) out.push({ kind: "finding", id: f.id, at: f.created_at, finding: f });
    for (const a of artifacts.data ?? []) {
      const t = outputTypeOf(a.type);
      if (t) out.push({ kind: "artifact", id: a.id, at: a.created_at, artifact: a, type: t });
    }
    for (const r of Array.isArray(prepared.data) ? prepared.data : []) {
      if (r.mode === "materialize" && r.status === "succeeded") out.push({ kind: "prepared", id: r.id, at: r.created_at, run: r });
    }
    return out.sort((a, b) => b.at.localeCompare(a.at));
  }, [findings.data, artifacts.data, prepared.data]);
  const counts = new Map<OutputType, number>();
  for (const i of items) counts.set(typeOf(i), (counts.get(typeOf(i)) ?? 0) + 1);
  const shown = items.filter((i) => !type || typeOf(i) === type);
  const open = selected ? items.find((i) => i.id === selected) : undefined;
  const loading = (artifacts.loading && !artifacts.data) || (findings.loading && !findings.data);
  const error = artifacts.error ?? findings.error;
  const filters = OUTPUT_FILTERS.filter((f) => counts.has(f.id) || f.id === type || f.id === "report" || f.id === "dashboard");

  return (
    <div className="page">
      <PageHeader title="Outputs" subtitle="Everything work produced, in one list: findings, dashboards, reports, datasets and prepared data." />
      <nav className="chip-row type-filter" aria-label="Filter outputs by type">
        <Link className={`chip ${type ? "" : "active"}`} aria-current={type ? undefined : "page"} to={to.outputs(wsId)}>All ({items.length})</Link>
        {filters.map((f) => (
          <Link key={f.id} className={`chip ${f.id === type ? "active" : ""}`} aria-current={f.id === type ? "page" : undefined}
            to={to.outputs(wsId, { type: f.id })}>{f.label} ({counts.get(f.id) ?? 0})</Link>
        ))}
      </nav>
      {type === "report" && (
        <details className="card generate-report">
          <summary>Generate a report</summary>
          <div className="card-body">
            <GenerateReportForm wsId={wsId} onCreated={(a) => { void artifacts.reload(); set({ artifact: a.id }); }} />
          </div>
        </details>
      )}
      <ErrorBox error={error} onRetry={() => { void artifacts.reload(); void findings.reload(); }} />
      {loading && <Loading />}
      {!loading && shown.length === 0 && (
        <EmptyState title={type ? `No ${OUTPUT_FILTERS.find((f) => f.id === type)?.label.toLowerCase()} yet` : "No outputs yet"}>
          Investigations produce findings, charts and dashboards; reports and prepared data are made from them.
        </EmptyState>
      )}
      {shown.length > 0 && (
        <div className="split">
          <div className="split-list">
            <ul className="list selectable" aria-label="Outputs">
              {shown.map((i) => {
                const active = i.id === selected;
                return (
                  <li key={`${i.kind}-${i.id}`}>
                    {i.kind === "finding"
                      ? <Link to={to.findings(wsId, i.id)} className={`list-button ${active ? "active" : ""}`} aria-current={active ? "true" : undefined}>
                        <OutputRow item={i} /></Link>
                      : <button type="button" className={`list-button ${active ? "active" : ""}`} aria-current={active ? "true" : undefined}
                        onClick={() => set(i.kind === "prepared" ? { run: i.id, artifact: null, dashboard: null } : { artifact: i.id, dashboard: null, run: null })}>
                        <OutputRow item={i} /></button>}
                  </li>
                );
              })}
            </ul>
          </div>
          <div className="split-detail">
            {insightId ? <FindingDetail id={insightId} wsId={wsId} />
              : open?.kind === "prepared" ? <PreparedView run={open.run} wsId={wsId} />
                : selected ? <ArtifactView id={selected} wsId={wsId} onSelect={(id) => set({ artifact: id })} />
                  : <EmptyState title="Select an output">A dashboard shows a live preview from its charts&apos; data before anything is published.</EmptyState>}
          </div>
        </div>
      )}
    </div>
  );
}

const TYPE_WORD: Record<OutputType, string> = {
  finding: "finding", dashboard: "dashboard", report: "report", dataset: "dataset", chart: "chart", prepared: "prepared data", other: "",
};

function OutputRow({ item: i }: { item: Item }) {
  if (i.kind === "finding") {
    const f = i.finding;
    const cause = voidCause(f.verification_state);
    return (
      <>
        <span className="list-button-head"><span className="clamp-1"><strong>{f.code}</strong> {f.title}</span><span className="muted small">finding</span></span>
        <span className="chip-row">
          {f.verification_state ? <VerificationBadge state={f.verification_state} showCause={false} />
            : <StatusBadge status={f.verified ? "verified" : f.status} label={f.verified ? "verified" : f.status} />}
        </span>
        {cause && <span className="small void-cause">Why void: {cause}</span>}
        <ConfidenceBar value={f.confidence} />
      </>
    );
  }
  if (i.kind === "prepared") {
    return (
      <>
        <span className="list-button-head"><span className="clamp-1">{i.run.recipe_name}</span>
          <span className="muted small">prepared data · v{i.run.recipe_version}</span></span>
        <span className="chip-row"><StatusBadge status={i.run.status} /><span className="muted small">{fmtDate(i.run.finished_at ?? i.run.created_at)}</span></span>
      </>
    );
  }
  const a = i.artifact;
  return (
    <>
      <span className="list-button-head"><span className="clamp-1">{a.name}</span>
        <span className="muted small">{TYPE_WORD[i.type] || a.type.replace(/_/g, " ")} · v{a.version}</span></span>
      <span className="chip-row"><StatusBadge status={a.status} />{a.platform && <span className="tag">{a.platform}</span>}
        <span className="muted small">{fmtDate(a.created_at)}</span></span>
    </>
  );
}

/** A materialized recipe run: its outputs, and a link back to the recipe in Work. */
function PreparedView({ run, wsId }: { run: RecipeRun; wsId: string }) {
  const outputs = Object.entries(run.outputs ?? {});
  return (
    <Card title={<>{run.recipe_name} <span className="muted small">· prepared data · v{run.recipe_version}</span></>} actions={<StatusBadge status={run.status} />}>
      <KeyValue items={[["Finished", fmtDate(run.finished_at)], ["Engine", run.engine ?? "—"], ["Outputs", String(outputs.length)]]} />
      {outputs.length > 0 && (
        <RecordTable records={outputs.map(([name, o]) => ({ output: name, ...(o && typeof o === "object" ? o as Record<string, unknown> : { value: o }) }))} />
      )}
      <p className="small"><Link to={to.work(wsId, "prepare", { recipe: run.recipe_id })}>Open the recipe in Work → Prepare data</Link></p>
      <TechnicalDetails value={run} />
    </Card>
  );
}

function ArtifactView({ id, wsId, onSelect }: { id: string; wsId: string; onSelect: (id: string) => void }) {
  const d = useAsync(() => api.getArtifact(id), [id]);
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />;
  if (!d.data) return <Loading />;
  const a = d.data;
  return (
    <div className="stack">
      <Card title={<>{a.name} <span className="muted small">· {a.type.replace(/_/g, " ")} · v{a.version}</span></>} actions={<>
        <StatusBadge status={a.status} />
        {a.external_url && <a className="btn btn-sm" href={a.external_url} target="_blank" rel="noreferrer noopener">Open in {a.platform ?? "BI"} ↗</a>}
      </>}>
        <KeyValue items={[
          ["Created by", a.creator_agent ? `agent:${a.creator_agent}` : a.creator_user ? `user:${a.creator_user}` : "—"],
          ["Investigation", a.run_id ? <Link key="r" to={to.run(wsId, a.run_id)}>Open the investigation</Link> : "—"],
          ["Updated", fmtDate(a.updated_at)],
          ...(a.external_id ? [["External id", `${a.platform}:${a.external_id}`] as [string, string]] : []),
        ]} />
        <TechnicalDetails><p className="small">Content hash <code title={a.content_hash}>{shortHash(a.content_hash, 16)}</code></p></TechnicalDetails>
      </Card>
      <ArtifactContent a={a} wsId={wsId} />
      <Card title={`Versions (${a.versions.length})`}>
        <ul className="list compact">
          {a.versions.map((v) => (
            <li key={v.id} className="list-item">
              <span><strong>v{v.version}</strong></span>
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
            ["Series", String(c.series ?? "—")], ["Findings", ((c.insight_codes as string[]) ?? []).join(", ") || "—"], ["Rationale", String(c.rationale ?? "—")],
          ]} />
        </Card>
      );
    case "dashboard":
      return <><PublishCard wsId={wsId} artifact={a} /><DashboardPreview wsId={wsId} artifact={a} /></>;
    case "report":
      return <ReportRow wsId={wsId} report={a} />;
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
          <p className="small">Metric definitions are edited in <Link to={to.data(wsId, "metrics")}>Data → Metrics</Link>.</p>
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
          <TechnicalDetails value={c} label="Full profile" />
        </Card>
      );
    }
    case "quality_report": {
      const issues = Array.isArray(c.issues) ? (c.issues as Record<string, unknown>[]) : [];
      return (
        <Card title={`Quality report — ${issues.length} issues`}>
          <RecordTable maxRows={200} records={issues.map((i) => ({ severity: i.severity, code: i.code, asset: i.asset, column: i.column, message: i.message }))} />
          <TechnicalDetails value={c} label="Full report" />
        </Card>
      );
    }
    default:
      return <Card title="Content"><TechnicalDetails value={c} label="Content" /></Card>;
  }
}
