/**
 * The information architecture (spec v3 §9): six journeys, at most 20 screens.
 *
 * SCREENS is the single route manifest. App.tsx renders exactly these routes, the side nav and the
 * command palette are built from it, and a test asserts the screen budget, so a new screen cannot
 * be added without being counted. LEGACY_REDIRECTS keeps every pre-increment-4 URL working.
 */

export type Journey = "home" | "ask" | "investigate" | "knowledge" | "build" | "operate" | "access";

export const JOURNEYS: { id: Exclude<Journey, "access">; label: string; description: string }[] = [
  { id: "home", label: "Home", description: "What changed since you were last here" },
  { id: "ask", label: "Ask", description: "Questions answered from governed data" },
  { id: "investigate", label: "Investigate", description: "Objectives, hypotheses and verified findings" },
  { id: "knowledge", label: "Knowledge", description: "Sources, the catalog, knowledge documents, review and the semantic model" },
  { id: "build", label: "Build", description: "Datasets, metrics, dashboards and reports" },
  { id: "operate", label: "Operate", description: "Approvals, monitors, schedules, policy, usage and platform admin" },
];

export type ScreenId =
  | "login"
  | "workspaces" | "workspace-home"
  | "ask"
  | "investigations" | "investigation" | "investigation-console" | "findings"
  | "sources" | "catalog"
  | "studio" | "reports"
  | "approvals" | "monitoring" | "schedules" | "governance" | "registry" | "settings" | "usage";

export interface Screen {
  id: ScreenId;
  journey: Journey;
  /** react-router pattern. */
  path: string;
  title: string;
  /** Shown in the side nav (screens with parameters other than :wsId are reached from lists). */
  nav: boolean;
  /** Needs a workspace (`:wsId`) in the URL. */
  workspace: boolean;
  keywords?: string;
}

export const SCREEN_BUDGET = 20;

export const SCREENS: Screen[] = [
  { id: "login", journey: "access", path: "/login", title: "Sign in", nav: false, workspace: false },

  { id: "workspaces", journey: "home", path: "/", title: "Workspaces", nav: true, workspace: false, keywords: "home landing" },
  { id: "workspace-home", journey: "home", path: "/w/:wsId", title: "Workspace home", nav: true, workspace: true,
    keywords: "overview what changed start analysis" },

  { id: "ask", journey: "ask", path: "/w/:wsId/ask", title: "Ask", nav: true, workspace: true, keywords: "question sql console explain" },

  { id: "investigations", journey: "investigate", path: "/w/:wsId/investigate", title: "Investigations", nav: true, workspace: true,
    keywords: "analysis runs objective" },
  { id: "findings", journey: "investigate", path: "/w/:wsId/investigate/findings/:insightId?", title: "Findings", nav: true, workspace: true,
    keywords: "insights verified evidence" },
  { id: "investigation", journey: "investigate", path: "/w/:wsId/investigate/:runId", title: "Investigation board", nav: false, workspace: true },
  { id: "investigation-console", journey: "investigate", path: "/w/:wsId/investigate/:runId/console", title: "Agent console", nav: false,
    workspace: true },

  { id: "catalog", journey: "knowledge", path: "/w/:wsId/knowledge/catalog", title: "Knowledge studio", nav: true, workspace: true,
    keywords: "catalog tables columns glossary descriptions okf documents review queue suggestions semantic graph metrics import export" },
  { id: "sources", journey: "knowledge", path: "/w/:wsId/knowledge/sources", title: "Sources & crawls", nav: true, workspace: true,
    keywords: "connect discover crawl drift data" },

  { id: "studio", journey: "build", path: "/w/:wsId/build/studio", title: "Studio", nav: true, workspace: true,
    keywords: "datasets metrics charts dashboards artifacts dbt builds kpis semantic publish" },
  { id: "reports", journey: "build", path: "/w/:wsId/build/reports", title: "Reports", nav: true, workspace: true },

  { id: "approvals", journey: "operate", path: "/w/:wsId/operate/approvals", title: "Approvals", nav: true, workspace: true,
    keywords: "inbox publish approve reject" },
  { id: "monitoring", journey: "operate", path: "/w/:wsId/operate/monitoring", title: "Monitors & alerts", nav: true, workspace: true },
  { id: "schedules", journey: "operate", path: "/w/:wsId/operate/schedules", title: "Schedules", nav: true, workspace: true },
  { id: "governance", journey: "operate", path: "/w/:wsId/operate/governance", title: "Policy & members", nav: true, workspace: true,
    keywords: "audit roles" },
  { id: "registry", journey: "operate", path: "/operate/registry", title: "Capability registry", nav: true, workspace: false,
    keywords: "agents tools skills models prompts" },
  { id: "settings", journey: "operate", path: "/operate/settings", title: "Platform settings", nav: true, workspace: false,
    keywords: "admin llm modes presets features limits" },
  { id: "usage", journey: "operate", path: "/operate/usage", title: "Usage & cost", nav: true, workspace: false,
    keywords: "token savings spend audit log" },
];

export function screen(id: ScreenId): Screen {
  const s = SCREENS.find((x) => x.id === id);
  if (!s) throw new Error(`unknown screen ${id}`);
  return s;
}

/**
 * Fill a route pattern. Optional segments without a value are dropped; a missing required value
 * (a record without its run id, say) also drops the segment, so the link lands on the parent list
 * rather than throwing during render.
 */
export function fillPath(pattern: string, params: Record<string, string | null | undefined> = {}): string {
  const segs: string[] = [];
  for (const seg of pattern.split("/")) {
    if (!seg.startsWith(":")) {
      segs.push(seg);
      continue;
    }
    const v = params[seg.slice(1).replace(/\?$/, "")];
    if (v === undefined || v === null || v === "") break;
    segs.push(encodeURIComponent(v));
  }
  return segs.join("/") || "/";
}

function withQuery(path: string, query: Record<string, string | undefined | null> = {}): string {
  const parts = Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `${path}?${parts.join("&")}` : path;
}

/** Typed links into the IA. Every in-app link goes through here, so a route move is one edit. */
export const to = {
  workspaces: () => "/",
  workspace: (ws: string) => fillPath(screen("workspace-home").path, { wsId: ws }),
  ask: (ws: string) => fillPath(screen("ask").path, { wsId: ws }),
  investigations: (ws: string) => fillPath(screen("investigations").path, { wsId: ws }),
  run: (ws: string, run: string) => fillPath(screen("investigation").path, { wsId: ws, runId: run }),
  runConsole: (ws: string, run: string) => fillPath(screen("investigation-console").path, { wsId: ws, runId: run }),
  findings: (ws: string, insight?: string) => fillPath(screen("findings").path, { wsId: ws, insightId: insight }),
  sources: (ws: string) => fillPath(screen("sources").path, { wsId: ws }),
  catalog: (ws: string) => fillPath(screen("catalog").path, { wsId: ws }),
  studio: (ws: string, artifact?: string) => withQuery(fillPath(screen("studio").path, { wsId: ws }), { artifact }),
  /** Build studio tabs (P4-U05): builds (dbt jobs), kpis (semantic layer), dashboards (preview → publish). */
  build: (ws: string, tab: "builds" | "kpis" | "dashboards", q: { job?: string; kpi?: string; dashboard?: string } = {}) =>
    withQuery(fillPath(screen("studio").path, { wsId: ws }), { tab, ...q }),
  /** Knowledge studio tabs (P4-U04); `doc` is a context receipt's document id, opened in Documents. */
  knowledge: (ws: string, tab?: "catalog" | "documents" | "review" | "graph" | "metrics" | "transfer",
    q: { pack?: string; path?: string; doc?: string } = {}) =>
    withQuery(fillPath(screen("catalog").path, { wsId: ws }), { tab: tab === "catalog" ? undefined : tab, ...q }),
  reports: (ws: string, artifact?: string) => withQuery(fillPath(screen("reports").path, { wsId: ws }), { artifact }),
  approvals: (ws: string) => fillPath(screen("approvals").path, { wsId: ws }),
  monitoring: (ws: string, q: { tab?: string; alert?: string } = {}) =>
    withQuery(fillPath(screen("monitoring").path, { wsId: ws }), { tab: q.tab, alert: q.alert }),
  schedules: (ws: string, schedule?: string) => withQuery(fillPath(screen("schedules").path, { wsId: ws }), { schedule }),
  governance: (ws: string) => fillPath(screen("governance").path, { wsId: ws }),
  registry: () => screen("registry").path,
  settings: () => screen("settings").path,
  usage: () => screen("usage").path,
};

/** Pre-increment-4 URLs → their new home. Query strings are carried over by the redirect. */
export const LEGACY_REDIRECTS: { from: string; to: string }[] = [
  { from: "/admin", to: "/operate/registry" },
  { from: "/operate", to: "/operate/registry" },
  { from: "/w/:wsId/sources", to: "/w/:wsId/knowledge/sources" },
  { from: "/w/:wsId/catalog", to: "/w/:wsId/knowledge/catalog" },
  { from: "/w/:wsId/knowledge", to: "/w/:wsId/knowledge/catalog" },
  { from: "/w/:wsId/runs", to: "/w/:wsId/investigate" },
  { from: "/w/:wsId/runs/:runId", to: "/w/:wsId/investigate/:runId" },
  { from: "/w/:wsId/runs/:runId/console", to: "/w/:wsId/investigate/:runId/console" },
  { from: "/w/:wsId/insights", to: "/w/:wsId/investigate/findings" },
  { from: "/w/:wsId/insights/:insightId", to: "/w/:wsId/investigate/findings/:insightId" },
  { from: "/w/:wsId/studio", to: "/w/:wsId/build/studio" },
  { from: "/w/:wsId/build", to: "/w/:wsId/build/studio" },
  { from: "/w/:wsId/reports", to: "/w/:wsId/build/reports" },
  { from: "/w/:wsId/schedules", to: "/w/:wsId/operate/schedules" },
  { from: "/w/:wsId/monitoring", to: "/w/:wsId/operate/monitoring" },
  { from: "/w/:wsId/governance", to: "/w/:wsId/operate/governance" },
  { from: "/w/:wsId/operate", to: "/w/:wsId/operate/approvals" },
];
