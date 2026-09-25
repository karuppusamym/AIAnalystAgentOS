import { useMemo, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, type Asset, type DiscoveredAsset, type Source, type SourceColumn } from "../api";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, PageHeader, StatusBadge, Tag } from "../components/ui";
import { fmtDate, fmtNumber, fmtPct, fmtValue } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";

const splitList = (s: string) => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
const TOGGLE_TAGS = ["pii", "restricted", "sensitive"] as const;

export function SourcesPage() {
  const { wsId = "" } = useParams();
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const assets = useAsync(() => api.listAssets(wsId), [wsId]);
  const rels = useAsync(() => api.relationships(wsId), [wsId]);
  const [showAdd, setShowAdd] = useState(false);
  const [activeAsset, setActiveAsset] = useState<string | null>(null);

  const reloadAll = () => {
    void sources.reload();
    void assets.reload();
    void rels.reload();
  };
  const asset = assets.data?.find((a) => a.id === activeAsset) ?? null;

  return (
    <div className="page">
      <PageHeader title="Sources & data explorer" subtitle="Connect sources, discover tables, select what agents may analyse, and tag sensitive columns."
        actions={<button type="button" className="btn btn-primary" onClick={() => setShowAdd((s) => !s)}>{showAdd ? "Close" : "Add source"}</button>} />
      {showAdd && <AddSource wsId={wsId} onAdded={() => { setShowAdd(false); reloadAll(); }} />}
      <ErrorBox error={sources.error} onRetry={sources.reload} />
      {sources.loading && !sources.data && <Loading />}
      {sources.data?.length === 0 && <EmptyState title="No sources yet">Add a ServiceNow instance, a PostgreSQL database or upload a CSV.</EmptyState>}
      {sources.data?.map((s) => (
        <SourceCard key={s.id} wsId={wsId} source={s} assets={(assets.data ?? []).filter((a) => a.source_id === s.id)}
          onChanged={reloadAll} onOpenAsset={setActiveAsset} activeAsset={activeAsset} />
      ))}
      {asset && <AssetDetail asset={asset} onTagged={(col) => assets.setData((prev) => prev?.map((a) => a.id !== asset.id ? a : {
        ...a, columns: a.columns.map((c) => (c.name === col.name ? { ...c, tags: col.tags } : c)),
      }))} onClose={() => setActiveAsset(null)} />}
      <Card title="Relationships">
        <ErrorBox error={rels.error} />
        {rels.data?.length === 0 && <EmptyState title="No relationships discovered yet">They are found by metadata discovery and profiling runs.</EmptyState>}
        {!!rels.data?.length && (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>From</th><th>To</th><th>Cardinality</th><th>Confidence</th><th>Validated</th><th>Origin</th></tr></thead>
              <tbody>
                {rels.data.map((r) => (
                  <tr key={r.id}>
                    <td><code>{r.from_asset}.{r.from_column}</code></td>
                    <td><code>{r.to_asset}.{r.to_column}</code></td>
                    <td>{r.cardinality}</td>
                    <td className="num">{fmtPct(r.confidence, 0)}</td>
                    <td>{r.validated ? <StatusBadge status="validated" /> : <span className="muted">no</span>}</td>
                    <td>{r.origin}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function SourceCard({ wsId, source, assets, onChanged, onOpenAsset, activeAsset }: {
  wsId: string; source: Source; assets: Asset[]; onChanged: () => void; onOpenAsset: (id: string) => void; activeAsset: string | null;
}) {
  const [discovered, setDiscovered] = useState<DiscoveredAsset[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(() => new Set(assets.filter((a) => a.selected).map((a) => a.name)));
  const [dirty, setDirty] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const discoverAct = useAction();
  const selectAct = useAction();

  // Keep selection in sync with server state until the user edits it.
  const serverSelected = useMemo(() => assets.filter((a) => a.selected).map((a) => a.name).sort().join("|"), [assets]);
  const [lastServer, setLastServer] = useState(serverSelected);
  if (!dirty && serverSelected !== lastServer) {
    setLastServer(serverSelected);
    setSelected(new Set(serverSelected ? serverSelected.split("|") : []));
  }

  const names = useMemo(() => {
    const byName = new Map<string, { name: string; rows: number | null; cols: number; description: string | null }>();
    for (const a of assets) byName.set(a.name, { name: a.name, rows: a.row_count, cols: a.columns.length, description: a.description ?? a.business_name });
    for (const d of discovered ?? []) if (!byName.has(d.name)) byName.set(d.name, { name: d.name, rows: d.row_count, cols: d.columns.length, description: d.description });
    return [...byName.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [assets, discovered]);

  const discover = async () => {
    const r = await discoverAct.run(() => api.discover(wsId, source.id));
    if (r) {
      setDiscovered(r.assets);
      setResult(`Discovered ${r.assets.length} assets.`);
      onChanged();
    }
  };
  const save = async () => {
    const r = await selectAct.run(() => api.selectAssets(wsId, source.id, [...selected]));
    if (r) {
      setDirty(false);
      setResult(`Selected ${r.selected.length} assets${r.loaded.length ? `; loaded ${r.loaded.length} into staging` : ""}.`);
      onChanged();
    }
  };
  const toggle = (name: string) => {
    setDirty(true);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <Card title={<>{source.name} <span className="muted small">· {source.kind} · {source.execution_mode}</span></>}
      actions={<>
        <StatusBadge status={source.status} />
        <button type="button" className="btn btn-sm" onClick={discover} disabled={discoverAct.busy}>{discoverAct.busy ? "Discovering…" : "Discover"}</button>
      </>}>
      <p className="muted small">
        {source.secret_ref ? <>Secret: <code>{source.secret_ref}</code> · </> : null}
        Last discovered {fmtDate(source.last_discovered_at)}{source.staging_schema ? <> · staging schema <code>{source.staging_schema}</code></> : null}
      </p>
      {source.last_error && <Notice tone="danger">{source.last_error}</Notice>}
      <ErrorBox error={discoverAct.error ?? selectAct.error} />
      {result && <Notice tone="success">{result}</Notice>}
      {names.length === 0 ? <EmptyState title="No assets discovered">Press Discover to list tables.</EmptyState> : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th scope="col"><span className="sr-only">Select</span></th><th>Asset</th><th className="num">Rows</th><th className="num">Columns</th><th>Description</th><th /></tr></thead>
              <tbody>
                {names.map((n) => {
                  const a = assets.find((x) => x.name === n.name);
                  return (
                    <tr key={n.name} className={a && a.id === activeAsset ? "row-active" : undefined}>
                      <td><input type="checkbox" aria-label={`Select ${n.name}`} checked={selected.has(n.name)} onChange={() => toggle(n.name)} /></td>
                      <td><code>{a?.fq ?? n.name}</code></td>
                      <td className="num">{fmtNumber(n.rows)}</td>
                      <td className="num">{n.cols}</td>
                      <td className="muted small clamp-1">{n.description ?? ""}</td>
                      <td>{a && <button type="button" className="btn btn-xs btn-ghost" onClick={() => onOpenAsset(a.id)}>Columns</button>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="form-actions">
            <span className="muted small">{selected.size} selected{dirty ? " (unsaved)" : ""}</span>
            <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={selectAct.busy || selected.size === 0}>
              {selectAct.busy ? "Saving & loading…" : "Save selection"}
            </button>
          </div>
        </>
      )}
    </Card>
  );
}

function AssetDetail({ asset, onTagged, onClose }: { asset: Asset; onTagged: (c: SourceColumn) => void; onClose: () => void }) {
  const act = useAction();
  const toggleTag = async (col: SourceColumn, tag: string) => {
    const tags = col.tags.includes(tag) ? col.tags.filter((t) => t !== tag) : [...col.tags, tag];
    const updated = await act.run(() => api.tagColumn(asset.id, col.name, tags));
    if (updated) onTagged(updated);
  };
  return (
    <Card title={<>Columns of <code>{asset.fq}</code></>} actions={<button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>Close</button>}>
      <p className="muted small">{fmtNumber(asset.row_count)} rows · source table <code>{asset.source_name}</code>. PII and restricted columns are
        withheld from agents and SQL according to workspace policy.</p>
      <ErrorBox error={act.error} />
      <div className="table-wrap">
        <table className="table table-compact">
          <thead>
            <tr><th>Column</th><th>Type</th><th>Semantic</th><th>Tags</th><th className="num">Null rate</th><th className="num">Distinct</th><th>Top values</th><th>Sensitivity</th></tr>
          </thead>
          <tbody>
            {asset.columns.map((c) => (
              <tr key={c.name}>
                <td><code>{c.name}</code>{c.is_key && <Tag tone="info">key</Tag>}{c.business_name && <div className="muted small">{c.business_name}</div>}</td>
                <td className="small">{c.data_type}</td>
                <td>{c.semantic_type ?? <span className="muted">—</span>}</td>
                <td>{c.tags.length ? c.tags.map((t) => <Tag key={t} tone={t === "pii" || t === "restricted" ? "danger" : "warning"}>{t}</Tag>) : <span className="muted">—</span>}</td>
                <td className="num">{c.profile?.null_rate !== undefined ? fmtPct(c.profile.null_rate) : "—"}</td>
                <td className="num">{c.profile?.distinct !== undefined ? fmtNumber(c.profile.distinct) : "—"}</td>
                <td className="small">
                  {(c.profile?.top_values ?? []).slice(0, 3).map((tv, i) => (
                    <div key={i} className="clamp-1">{fmtValue(tv.value)} <span className="muted">({fmtNumber(tv.count)})</span></div>
                  ))}
                  {!c.profile?.top_values?.length && <span className="muted">not profiled</span>}
                </td>
                <td>
                  <div className="toggle-group">
                    {TOGGLE_TAGS.map((t) => (
                      <label key={t} className="toggle small">
                        <input type="checkbox" checked={c.tags.includes(t)} disabled={act.busy} onChange={() => toggleTag(c, t)} /> {t}
                      </label>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function AddSource({ wsId, onAdded }: { wsId: string; onAdded: () => void }) {
  const [kind, setKind] = useState<"servicenow" | "postgres" | "csv">("servicenow");
  const [name, setName] = useState("");
  const [secretRef, setSecretRef] = useState("env:SERVICENOW_PASSWORD");
  // servicenow
  const [instanceUrl, setInstanceUrl] = useState("");
  const [username, setUsername] = useState("");
  const [tables, setTables] = useState("incident, problem, change_request");
  const [pageSize, setPageSize] = useState(1000);
  const [maxRows, setMaxRows] = useState(200000);
  // postgres
  const [host, setHost] = useState("");
  const [port, setPort] = useState(5432);
  const [database, setDatabase] = useState("");
  const [schemas, setSchemas] = useState("public");
  // csv
  const [file, setFile] = useState<File | null>(null);
  const act = useAction();

  const changeKind = (k: typeof kind) => {
    setKind(k);
    setSecretRef(k === "servicenow" ? "env:SERVICENOW_PASSWORD" : k === "postgres" ? "env:POSTGRES_SOURCE_PASSWORD" : "");
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const ok = await act.run(async () => {
      let config: Record<string, unknown>;
      if (kind === "servicenow") {
        config = { instance_url: instanceUrl.trim(), username: username.trim(), tables: splitList(tables), page_size: pageSize, max_rows: maxRows };
      } else if (kind === "postgres") {
        config = { host: host.trim(), port, database: database.trim(), username: username.trim(), schemas: splitList(schemas) };
      } else {
        if (!file) throw new Error("choose a .csv, .parquet or .xlsx file");
        const up = await api.upload(wsId, file);
        config = { path: up.path };
      }
      if (!instanceUrl.trim() && kind === "servicenow") delete config.instance_url; // server falls back to the configured mock
      return api.addSource(wsId, { kind, name: name.trim() || (file?.name ?? kind), config, secret_ref: secretRef.trim() || null });
    });
    if (ok) onAdded();
  };

  return (
    <Card title="Add source">
      <form className="form" onSubmit={submit}>
        <div className="form-row">
          <Field label="Kind" htmlFor="src-kind">
            <select id="src-kind" value={kind} onChange={(e) => changeKind(e.target.value as typeof kind)}>
              <option value="servicenow">ServiceNow</option>
              <option value="postgres">PostgreSQL</option>
              <option value="csv">File upload (CSV / Parquet / XLSX)</option>
            </select>
          </Field>
          <Field label="Name" htmlFor="src-name">
            <input id="src-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Production ITSM" />
          </Field>
        </div>
        {kind === "servicenow" && (
          <>
            <div className="form-row">
              <Field label="Instance URL" htmlFor="sn-url" hint="Leave empty to use the configured demo/mock instance.">
                <input id="sn-url" type="url" value={instanceUrl} onChange={(e) => setInstanceUrl(e.target.value)} placeholder="https://example.service-now.com" />
              </Field>
              <Field label="Username" htmlFor="sn-user"><input id="sn-user" value={username} onChange={(e) => setUsername(e.target.value)} /></Field>
            </div>
            <Field label="Tables" htmlFor="sn-tables" hint="Comma separated.">
              <input id="sn-tables" value={tables} onChange={(e) => setTables(e.target.value)} />
            </Field>
            <div className="form-row">
              <Field label="Page size" htmlFor="sn-page"><input id="sn-page" type="number" min={1} max={10000} value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))} /></Field>
              <Field label="Max rows" htmlFor="sn-max"><input id="sn-max" type="number" min={1} value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value))} /></Field>
            </div>
          </>
        )}
        {kind === "postgres" && (
          <>
            <div className="form-row">
              <Field label="Host" htmlFor="pg-host"><input id="pg-host" required value={host} onChange={(e) => setHost(e.target.value)} /></Field>
              <Field label="Port" htmlFor="pg-port"><input id="pg-port" type="number" value={port} onChange={(e) => setPort(Number(e.target.value))} /></Field>
            </div>
            <div className="form-row">
              <Field label="Database" htmlFor="pg-db"><input id="pg-db" required value={database} onChange={(e) => setDatabase(e.target.value)} /></Field>
              <Field label="Username" htmlFor="pg-user"><input id="pg-user" required value={username} onChange={(e) => setUsername(e.target.value)} /></Field>
            </div>
            <Field label="Schemas" htmlFor="pg-schemas" hint="Comma separated."><input id="pg-schemas" value={schemas} onChange={(e) => setSchemas(e.target.value)} /></Field>
          </>
        )}
        {kind === "csv" && (
          <Field label="File" htmlFor="csv-file" hint="Uploaded to the workspace (max 200 MB), then registered with config.path.">
            <input id="csv-file" type="file" accept=".csv,.parquet,.xlsx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </Field>
        )}
        {kind !== "csv" && (
          <Field label="Secret reference" htmlFor="src-secret"
            hint={<>A reference, never the secret itself: <code>env:NAME</code> or <code>file:/path</code>. The server resolves it at connect time.</>}>
            <input id="src-secret" value={secretRef} onChange={(e) => setSecretRef(e.target.value)} placeholder="env:SERVICENOW_PASSWORD"
              pattern="^(env:[A-Za-z_][A-Za-z0-9_]*|file:/.+)?$" autoComplete="off" />
          </Field>
        )}
        <ErrorBox error={act.error} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Adding…" : "Add source"}</button>
        </div>
      </form>
    </Card>
  );
}
