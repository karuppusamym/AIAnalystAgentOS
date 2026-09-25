import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type CapabilityManifest, type CapabilityReload, type CapabilitySummary, type Dict } from "../api";
import { ResultView } from "../components/renderers";
import { SchemaForm } from "../components/SchemaForm";
import { Card, EmptyState, EnabledToggle, ErrorBox, KeyValue, Loading, Notice, StateView, Tag, TechnicalDetails } from "../components/ui";
import {
  certTone, groupByKind, invoke, kindLabel, loadManifest, matches, selfGoverned, sideEffectInfo, type InvokeOutcome,
} from "../lib/capabilities";
import { useAction, useAsync } from "../lib/hooks";
import { isEmptySchema, type Schema } from "../lib/jsonSchema";
import { to } from "../routes";

/** Kinds a person can run from the registry; the rest run inside playbooks, crawls or publication. */
const RUNNABLE_KINDS = new Set(["Tool", "Method", "Skill", "Detector"]);

/**
 * Operate → Capability registry, generated from GET /api/capabilities (P4-U06). Nothing here is
 * specific to a kind: a newly installed plugin, even one of a kind this UI predates, appears in
 * its own group with its certification and side-effect badges, and (P4-U07) gets a form generated
 * from its `input_schema` and a result view chosen by its `ui.renderer`.
 */
export function CapabilityRegistry({ isAdmin }: { isAdmin: boolean }) {
  const [params, setParams] = useSearchParams();
  const ws = params.get("ws") || "";
  const selected = params.get("cap");
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<string>("");
  const workspaces = useAsync(() => api.listWorkspaces(), []);
  const wsDetail = useAsync(() => (ws ? api.getWorkspace(ws) : Promise.resolve(null)), [ws]);
  const list = useAsync(() => api.listCapabilities(ws ? { workspace_id: ws } : {}), [ws]);
  const toggle = useAction();
  const reload = useAction();
  const [reloaded, setReloaded] = useState<CapabilityReload | null>(null);
  const canToggle = isAdmin || wsDetail.data?.role === "owner";

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const caps = list.data?.capabilities ?? [];
  const kinds = useMemo(() => groupByKind(caps).map((g) => ({ kind: g.kind, label: g.label, n: g.items.length })), [caps]);
  const groups = useMemo(() => groupByKind(caps.filter((c) => (!kind || c.kind === kind) && matches(c, q))), [caps, kind, q]);
  const current = caps.find((c) => c.id === selected) ?? null;

  const setEnabled = async (c: CapabilitySummary, enabled: boolean) => {
    const r = await toggle.run(() => api.setCapabilityEnabled(ws, c.id, enabled));
    if (r !== undefined) {
      list.setData((prev) => prev && { ...prev, capabilities: prev.capabilities.map((x) => (x.id === c.id ? { ...x, enabled } : x)) });
    }
  };
  const doReload = async () => {
    const r = await reload.run(() => api.reloadCapabilities());
    if (r) {
      setReloaded(r);
      void list.reload();
    }
  };

  return (
    <div className="stack">
      <Card>
        <div className="toolbar">
          <div className="chip-row">
            <label className="inline-field small">Workspace
              <select value={ws} onChange={(e) => setParam("ws", e.target.value || null)} aria-label="Workspace for enablement">
                <option value="">Platform (no workspace)</option>
                {(workspaces.data ?? []).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            </label>
            <input type="search" placeholder="Search id, summary, tag…" aria-label="Search capabilities" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="chip-row">
            {list.data && <span className="muted small" title={list.data.digest}>{caps.length} installed · registry <code>{list.data.digest.slice(0, 10)}</code></span>}
            {isAdmin && (
              <button type="button" className="btn btn-sm" onClick={() => void doReload()} disabled={reload.busy}>{reload.busy ? "Reloading…" : "Reload registry"}</button>
            )}
          </div>
        </div>
        <div className="chip-row registry-kinds" role="group" aria-label="Filter by kind">
          <button type="button" className={`chip ${kind === "" ? "active" : ""}`} aria-pressed={kind === ""} onClick={() => setKind("")}>All</button>
          {kinds.map((k) => (
            <button key={k.kind} type="button" className={`chip ${kind === k.kind ? "active" : ""}`} aria-pressed={kind === k.kind}
              onClick={() => setKind(kind === k.kind ? "" : k.kind)}>{k.label} · {k.n}</button>
          ))}
        </div>
        {ws && !canToggle && wsDetail.data && (
          <p className="muted small">Only workspace owners and platform admins can enable or disable capabilities (your role: {wsDetail.data.role}).</p>
        )}
        {!ws && <p className="muted small">Choose a workspace to see and change where each capability is enabled. Capabilities are disabled by default outside the built-in investigation set.</p>}
        <ErrorBox error={reload.error} />
        {reloaded && (
          <Notice tone={reloaded.problems.length ? "warning" : "success"}>
            Registry reloaded: {reloaded.count} capabilities, digest <code>{reloaded.digest.slice(0, 10)}</code>
            {reloaded.previous_digest === reloaded.digest ? " (unchanged)" : ` (was ${reloaded.previous_digest.slice(0, 10)})`}.
            {reloaded.problems.length > 0 && <ul className="small">{reloaded.problems.map((p) => <li key={p}>{p}</li>)}</ul>}
          </Notice>
        )}
        <ErrorBox error={toggle.error} />
      </Card>

      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading label="Loading capabilities…" />}

      <div className={`registry-grid ${current ? "has-detail" : ""}`}>
        <div className="stack">
          {list.data && groups.length === 0 && <EmptyState title={caps.length ? "No capability matches" : "No capabilities installed"} />}
          {groups.map((g) => (
            <Card key={g.kind} title={`${g.label} (${g.items.length})`} className="registry-group">
              <div className="table-wrap">
                <table className="table table-compact" aria-label={g.label}>
                  <thead><tr><th>Capability</th><th>Certification</th><th>Side effect</th><th>Runs as</th><th>Enabled</th><th><span className="sr-only">Actions</span></th></tr></thead>
                  <tbody>
                    {g.items.map((c) => {
                      const se = sideEffectInfo(c.side_effect);
                      return (
                        <tr key={c.id} className={c.id === selected ? "row-active" : undefined}>
                          <td>
                            <code>{c.id}</code> <span className="muted small">v{c.version}</span>
                            {c.source !== "builtin" && <Tag tone="info">{c.source}</Tag>}
                            <div className="muted small clamp-2">{c.summary}</div>
                          </td>
                          <td><Tag tone={certTone(c.certification.status)}>{c.certification.status}</Tag></td>
                          <td><span title={se.hint}><Tag tone={se.tone}>{se.label}</Tag></span>{c.needs_approval && <div className="muted small">needs approval</div>}</td>
                          <td className="small">{c.determinism} · {c.cost_class}</td>
                          <td>
                            {!ws ? <span className="muted small">—</span>
                              : selfGoverned(c) ? <span className="muted small">{c.source.startsWith("mcp:") ? "MCP allowlist" : "governed by source"}</span>
                                : c.enabled === null ? <span className="muted small">unknown</span>
                                  : <EnabledToggle enabled={c.enabled} disabled={!canToggle || toggle.busy} onChange={(v) => void setEnabled(c, v)}
                                    label={`Enable ${c.id} in this workspace`} />}
                          </td>
                          <td>
                            <button type="button" className="btn btn-xs btn-ghost" aria-pressed={c.id === selected}
                              aria-label={`Open ${c.id}`} onClick={() => setParam("cap", c.id === selected ? null : c.id)}>
                              {c.id === selected ? "Close" : "Open"}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>
          ))}
        </div>
        {current && <CapabilityDetail key={`${current.id}:${ws}`} summary={current} ws={ws || null} onClose={() => setParam("cap", null)} />}
      </div>
    </div>
  );
}

function CapabilityDetail({ summary, ws, onClose }: { summary: CapabilitySummary; ws: string | null; onClose: () => void }) {
  const m = useAsync(() => loadManifest(summary, ws), [summary.id, ws]);
  const se = sideEffectInfo(summary.side_effect);
  return (
    <Card className="registry-detail" title={<><code>{summary.id}</code> <span className="muted small">v{summary.version}</span></>}
      actions={<button type="button" className="btn btn-xs btn-ghost" onClick={onClose}>Close</button>}>
      <p>{summary.summary}</p>
      <div className="chip-row">
        <Tag>{kindLabel(summary.kind)}</Tag>
        <Tag tone={certTone(summary.certification.status)}>{summary.certification.status}</Tag>
        <Tag tone={se.tone}>{se.label}</Tag>
        {summary.autonomous_ok ? <Tag tone="success">runs under autonomy</Tag> : <Tag>manual runs only</Tag>}
      </div>
      <ErrorBox error={m.error} onRetry={m.reload} />
      {!m.data && !m.error && <Loading label="Loading manifest…" />}
      {m.data && <ManifestBody manifest={m.data} summary={summary} ws={ws} />}
    </Card>
  );
}

function ManifestBody({ manifest: m, summary, ws }: { manifest: CapabilityManifest; summary: CapabilitySummary; ws: string | null }) {
  const schema = (m.input_schema ?? {}) as Schema;
  const form = m.ui?.form ?? "auto";
  const runnable = RUNNABLE_KINDS.has(m.kind) || !isEmptySchema(schema);
  return (
    <div className="stack">
      <KeyValue items={[
        ["Source", summary.source],
        ["Entry", m.entry ? <code key="e">{m.entry}</code> : "—"],
        ["Determinism", m.determinism ?? summary.determinism],
        ["Cost class", m.cost_class ?? summary.cost_class],
        ["Permissions", (m.permissions ?? []).join(", ") || "—"],
        ["Requires", (m.requires ?? []).join(", ") || "—"],
        ["Certification evidence", m.certification?.evidence ? <code key="c">{m.certification.evidence}</code> : "none recorded"],
        ["Result view", m.ui?.renderer ? <code key="r">{m.ui.renderer}</code> : "inferred from the result"],
      ]} />
      {m.tags?.length ? <div className="chip-row">{m.tags.map((t) => <Tag key={t}>{t}</Tag>)}</div> : null}
      {runnable && form !== "none" && (
        <section aria-label="Run this capability" className="registry-run">
          <h3>Run</h3>
          {form === "custom" ? (
            <StateView kind="unknown" title="This capability ships its own form">It is used from the screen that owns it; the registry cannot generate one.</StateView>
          ) : !ws ? (
            <StateView kind="empty" title="Choose a workspace to run it">Runs are governed per workspace: scope, policy and enablement apply.</StateView>
          ) : summary.enabled === false ? (
            <StateView kind="not-entitled" title="Disabled in this workspace">An owner can enable it with the toggle in the list.</StateView>
          ) : (
            <RunPanel manifest={m} ws={ws} schema={schema} />
          )}
        </section>
      )}
      <TechnicalDetails value={m} label="Technical details (manifest)" />
    </div>
  );
}

function RunPanel({ manifest: m, ws, schema }: { manifest: CapabilityManifest; ws: string; schema: Schema }) {
  const act = useAction();
  const [outcome, setOutcome] = useState<InvokeOutcome | null>(null);
  const writes = sideEffectInfo(m.side_effect).tone === "danger";
  const submit = async (args: Record<string, unknown>) => {
    setOutcome(null);
    const r = await act.run(() => invoke(m, ws, args as Dict));
    if (r) setOutcome(r);
  };
  return (
    <>
      {writes && (
        <Notice tone="warning">This capability writes outside the platform. Running it requests an approval bound to the exact inputs; nothing is written until a person approves.</Notice>
      )}
      <SchemaForm schema={schema} onSubmit={(v) => void submit(v)} busy={act.busy} submitLabel={writes ? "Request run" : "Run"} />
      <ErrorBox error={act.error} />
      {outcome?.state === "ok" && (
        <div className="registry-result" aria-live="polite">
          <h3>Result</h3>
          <ResultView rendererId={m.ui?.renderer} value={outcome.result} title={m.summary} />
        </div>
      )}
      {outcome?.state === "approval" && (
        <StateView kind="refused" title="Waiting for approval"
          action={<Link to={to.approvals(ws)}>Open the approval inbox</Link>}>
          The run was held because of its side effect{outcome.approvalId ? <> (approval <code>{outcome.approvalId}</code>)</> : null}.
        </StateView>
      )}
      {outcome?.state === "error" && <StateView kind="failed" title="The capability failed">{outcome.message}</StateView>}
      {outcome?.state === "unsupported" && <StateView kind="unknown" title="Not runnable from here yet">{outcome.message}</StateView>}
    </>
  );
}
