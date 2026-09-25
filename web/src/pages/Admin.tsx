import { useState } from "react";
import { api, type AgentSpec, type ToolSpec } from "../api";
import { useAuth } from "../auth";
import { Card, EmptyState, EnabledToggle, ErrorBox, Loading, Notice, PageHeader, StatusBadge, Tabs, Tag, TechnicalDetails, Value } from "../components/ui";
import { fmtDate, fmtMs, fmtUsd } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { PromptsView, SettingsEditor, TokenSavingsView } from "./AdminSettings";
import { CapabilityRegistry } from "./Registry";

type Tab = "capabilities" | "agents" | "tools" | "skills" | "models" | "settings" | "savings" | "prompts" | "usage" | "audit";

/** Tabs whose endpoints are admin-only: rendered as a notice for everyone else instead of a 403. */
const ADMIN_ONLY = new Set<Tab>(["settings", "savings", "prompts", "usage", "audit"]);

export type AdminSection = "registry" | "settings" | "usage";

/** The Operate journey's platform screens: one screen per section, tabs within it. */
const SECTIONS: Record<AdminSection, { title: string; subtitle: string; tabs: { id: Tab; label: string }[] }> = {
  registry: {
    title: "Capability registry",
    subtitle: "Every installed capability — playbooks, agents, methods, tools, connectors, plugins and MCP tools — with certification, side effects and per-workspace enablement.",
    tabs: [{ id: "capabilities", label: "Capabilities" }, { id: "agents", label: "Agents" }, { id: "tools", label: "Tools" }, { id: "skills", label: "Skills" },
      { id: "models", label: "Models" }, { id: "prompts", label: "Prompts" }],
  },
  settings: {
    title: "Platform settings",
    subtitle: "Versioned runtime settings: LLM mode per purpose, presets, feature flags, limits and enabled source kinds.",
    tabs: [{ id: "settings", label: "Settings" }],
  },
  usage: {
    title: "Usage & cost",
    subtitle: "Tokens avoided, model spend by purpose, query gateway activity and the platform audit log.",
    tabs: [{ id: "savings", label: "Token savings" }, { id: "usage", label: "Usage" }, { id: "audit", label: "Audit log" }],
  },
};

export function AdminPage({ section = "registry" }: { section?: AdminSection }) {
  const { user } = useAuth();
  const spec = SECTIONS[section];
  const [tab, setTab] = useState<Tab>(spec.tabs[0].id);
  const tabbed = spec.tabs.length > 1;
  return (
    <div className="page">
      <PageHeader title={spec.title} subtitle={spec.subtitle} />
      {!user?.is_admin && section === "registry" && (
        <Notice tone="info">You can view the registries; changing them, platform settings, usage and the audit log need an admin account.</Notice>)}
      {tabbed && <Tabs value={tab} onChange={setTab} tabs={spec.tabs} />}
      <div className="tab-panel" role={tabbed ? "tabpanel" : undefined}>
        {ADMIN_ONLY.has(tab) && !user?.is_admin ? (
          <Notice tone="warning">This section is available to platform administrators only.</Notice>
        ) : (
          <>
            {tab === "capabilities" && <CapabilityRegistry isAdmin={!!user?.is_admin} />}
            {tab === "agents" && <Agents canEdit={!!user?.is_admin} />}
            {tab === "tools" && <Tools canEdit={!!user?.is_admin} />}
            {tab === "skills" && <Skills />}
            {tab === "models" && <Models />}
            {tab === "settings" && <SettingsEditor />}
            {tab === "savings" && <TokenSavingsView />}
            {tab === "prompts" && <PromptsView />}
            {tab === "usage" && <UsageView />}
            {tab === "audit" && <Audit />}
          </>
        )}
      </div>
    </div>
  );
}

function Agents({ canEdit }: { canEdit: boolean }) {
  const list = useAsync(() => api.agents(), []);
  const act = useAction();
  const toggle = async (a: AgentSpec, enabled: boolean) => {
    const r = await act.run(() => api.patchAgent(a.id, enabled));
    if (r) list.setData((prev) => prev?.map((x) => (x.id === a.id ? { ...x, enabled: r.enabled } : x)));
  };
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  return (
    <Card>
      <ErrorBox error={act.error} />
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Agent</th><th>Phase</th><th>Model profile</th><th>Tools / skills</th><th>Verification</th><th>Enabled</th></tr></thead>
          <tbody>
            {list.data.map((a) => (
              <tr key={a.id}>
                <td><strong>{a.name}</strong> <code className="small">{a.id}</code> <span className="muted small">v{a.version}</span><div className="muted small">{a.description}</div></td>
                <td><Tag tone={a.phase === "mvp" ? "info" : "neutral"}>{a.phase}</Tag></td>
                <td><code className="small">{a.model_profile}</code></td>
                <td className="small">{a.tools.length} tools · {a.skills.length} skills</td>
                <td>{a.verification_required ? <Tag tone="success">required</Tag> : <span className="muted">—</span>}</td>
                <td><EnabledToggle enabled={a.enabled} disabled={!canEdit || act.busy} onChange={(v) => toggle(a, v)} label={`Enable agent ${a.id}`} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Tools({ canEdit }: { canEdit: boolean }) {
  const list = useAsync(() => api.tools(), []);
  const act = useAction();
  const toggle = async (t: ToolSpec, enabled: boolean) => {
    const r = await act.run(() => api.patchTool(t.tool_id, enabled));
    if (r) list.setData((prev) => prev?.map((x) => (x.tool_id === t.tool_id ? { ...x, enabled: r.enabled } : x)));
  };
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  return (
    <Card>
      <ErrorBox error={act.error} />
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Tool</th><th>Category</th><th>Risk</th><th>Approval policy</th><th>Side effects</th><th>Min role</th><th>Enabled</th></tr></thead>
          <tbody>
            {list.data.map((t) => (
              <tr key={t.tool_id}>
                <td><code>{t.tool_id}</code><div className="muted small">{t.description}</div></td>
                <td>{t.category}</td>
                <td><Tag tone={t.risk === "high" ? "danger" : t.risk === "medium" ? "warning" : "neutral"}>{t.risk}</Tag></td>
                <td>{t.approval_policy === "none" ? <span className="muted">none</span> : <Tag tone="warning">{t.approval_policy}</Tag>}</td>
                <td className="small">{t.side_effects}</td>
                <td className="small">{t.min_role}</td>
                <td><EnabledToggle enabled={t.enabled} disabled={!canEdit || act.busy} onChange={(v) => toggle(t, v)} label={`Enable tool ${t.tool_id}`} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Skills() {
  const list = useAsync(() => api.skills(), []);
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  if (!list.data.length) return <EmptyState title="No skills registered" />;
  return (
    <Card>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Skill</th><th>Category</th><th>Tools</th><th>Deterministic</th><th>Status</th></tr></thead>
          <tbody>
            {list.data.map((s) => (
              <tr key={s.id}>
                <td><code>{s.id}</code><div className="muted small">{s.description}</div></td>
                <td>{s.category}</td>
                <td className="small">{s.tools.join(", ") || "—"}</td>
                <td>{s.deterministic ? "yes" : "no"}</td>
                <td><StatusBadge status={s.enabled ? "ok" : "skipped"} label={s.enabled ? "enabled" : "disabled"} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Models() {
  const m = useAsync(() => api.models(), []);
  if (m.error) return <ErrorBox error={m.error} onRetry={m.reload} />;
  if (!m.data) return <Loading />;
  const d = m.data;
  const decisionProfiles = new Set(Object.entries(d.profiles).filter(([, p]) => d.providers[p.provider]?.kind === "decision").map(([k]) => k));
  return (
    <div className="stack">
      <Card title="Routing: purpose → profile → models">
        <p className="muted small">Agents request a purpose; the router picks the first allowed, available model of its profile (fail closed).
          <span className="tag tag-jev">JEV decision</span> purposes use the TypeSafe Jev decision model: typed choices with probabilities.</p>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Purpose</th><th>Profile</th><th>Models (fallback order)</th><th>Mode</th><th>Available</th></tr></thead>
            <tbody>
              {Object.entries(d.routing).map(([purpose, profile]) => {
                const p = d.profiles[profile];
                const jev = decisionProfiles.has(profile);
                return (
                  <tr key={purpose} className={jev ? "row-jev" : undefined}>
                    <td><code>{purpose}</code>{jev && <span className="tag tag-jev">JEV decision</span>}</td>
                    <td><code className="small">{profile}</code>{p?.exclude_families?.length ? <div className="muted small">excludes: {p.exclude_families.join(", ")}</div> : null}</td>
                    <td className="small">{(d.effective?.[purpose]?.models ?? p?.models)?.join(" → ") ?? "—"}</td>
                    <td className="small">{d.effective?.[purpose]?.mode ?? "always"}{d.effective?.[purpose]?.deterministic_path && <div className="muted small">rule path</div>}</td>
                    <td><StatusBadge status={d.available[purpose] ? "ok" : "failed"} label={d.available[purpose] ? "available" : "unavailable"} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
      <div className="grid-2">
        <Card title="Providers">
          <ul className="list compact">
            {Object.entries(d.providers).map(([k, p]) => (
              <li key={k} className="list-item"><span><strong>{k}</strong> <Tag tone={p.kind === "decision" ? "info" : "neutral"}>{p.kind}</Tag></span><code className="small">{p.base_url}</code></li>
            ))}
          </ul>
        </Card>
        <Card title="Allowlist">
          <div className="chip-row">{d.allowlist.map((a) => <span key={a} className={`tag ${a.startsWith("typesafe/") ? "tag-jev" : ""}`}>{a}</span>)}</div>
        </Card>
      </div>
      <Card title="Profiles"><TechnicalDetails value={d.profiles} label="Profile settings" /></Card>
    </div>
  );
}

function UsageView() {
  const u = useAsync(() => api.usage(), []);
  if (u.error) return <ErrorBox error={u.error} onRetry={u.reload} />;
  if (!u.data) return <Loading />;
  const total = u.data.models.reduce((s, m) => s + m.cost_usd, 0);
  const calls = u.data.models.reduce((s, m) => s + m.calls, 0);
  return (
    <div className="stack">
      <Card title={`Model usage — ${calls.toLocaleString()} calls, ${fmtUsd(total)}`}>
        {u.data.models.length === 0 ? <EmptyState title="No model calls yet" /> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Purpose</th><th>Provider / model</th><th className="num">Calls</th><th className="num">Failed</th><th className="num">Avg latency</th><th className="num">Cost</th></tr></thead>
              <tbody>
                {u.data.models.map((m, i) => (
                  <tr key={i} className={m.provider === "typesafe" ? "row-jev" : undefined}>
                    <td><code>{m.purpose}</code>{m.provider === "typesafe" && <span className="tag tag-jev">JEV</span>}</td>
                    <td className="small">{m.provider} / {m.model}</td>
                    <td className="num"><Value value={m.calls} format="int" /></td>
                    <td className="num"><Value value={m.failed} format="int" /></td>
                    <td className="num"><Value value={m.avg_latency_ms} format="ms" /></td>
                    <td className="num"><Value value={m.cost_usd} format="usd" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Card title="Query gateway">
        {u.data.queries.length === 0 ? <EmptyState title="No queries yet" /> : (
          <div className="chip-row">
            {u.data.queries.map((q) => <span key={q.status} className="stat-inline"><StatusBadge status={q.status} /> {q.count.toLocaleString()} · avg {fmtMs(q.avg_ms)}</span>)}
          </div>
        )}
      </Card>
    </div>
  );
}

function Audit() {
  const a = useAsync(() => api.audit(300), []);
  const [q, setQ] = useState("");
  if (a.error) return <ErrorBox error={a.error} onRetry={a.reload} />;
  if (!a.data) return <Loading />;
  const needle = q.toLowerCase();
  const rows = a.data.filter((e) => !needle || `${e.actor} ${e.action} ${e.target ?? ""} ${e.decision ?? ""} ${e.workspace_id ?? ""}`.toLowerCase().includes(needle));
  return <AuditTable rows={rows} filter={q} onFilter={setQ} />;
}

export function AuditTable({ rows, filter, onFilter }: { rows: import("../api").AuditEvent[]; filter: string; onFilter: (s: string) => void }) {
  return (
    <Card title={`Audit events (${rows.length})`} actions={
      <input type="search" placeholder="Filter actor, action, target…" aria-label="Filter audit events" value={filter} onChange={(e) => onFilter(e.target.value)} />}>
      {rows.length === 0 ? <EmptyState title="No audit events" /> : (
        <div className="table-wrap">
          <table className="table table-compact">
            <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Decision</th><th>Details</th></tr></thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id}>
                  <td className="small">{fmtDate(e.created_at)}</td>
                  <td className="small"><code>{e.actor}</code></td>
                  <td><code>{e.action}</code></td>
                  <td className="small">{e.target ?? "—"}</td>
                  <td>{e.decision ? <StatusBadge status={e.decision} /> : "—"}{e.reasons?.length ? <div className="muted small">{e.reasons.join(", ")}</div> : null}</td>
                  <td>{e.details && Object.keys(e.details).length ? <TechnicalDetails value={e.details} label="details" /> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
