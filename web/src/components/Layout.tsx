import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useMatch } from "react-router-dom";
import { api, type Workspace } from "../api";
import { useAuth } from "../auth";
import { nextThemePref, setThemePref, useTheme } from "../lib/theme";
import { fillPath, JOURNEYS, SCREENS } from "../routes";
import { CommandPalette } from "./CommandPalette";
import { ErrorBoundary } from "./ErrorBoundary";
import { NotificationBell } from "./NotificationBell";

const THEME_LABEL = { system: "System theme", light: "Light theme", dark: "Dark theme" } as const;
const THEME_ICON = { system: "◐", light: "☀", dark: "☾" } as const;

export function ThemeToggle() {
  const { pref } = useTheme();
  const next = nextThemePref(pref);
  return (
    <button type="button" className="btn btn-sm btn-ghost" onClick={() => setThemePref(next)}
      title={`${THEME_LABEL[pref]} — switch to ${THEME_LABEL[next].toLowerCase()}`}>
      <span aria-hidden="true">{THEME_ICON[pref]}</span>
      <span className="sr-only">{THEME_LABEL[pref]}; switch to {THEME_LABEL[next].toLowerCase()}</span>
    </button>
  );
}

function useWorkspace(wsId: string | undefined): Workspace | null {
  const [ws, setWs] = useState<Workspace | null>(null);
  useEffect(() => {
    if (!wsId) return setWs(null);
    let alive = true;
    api.getWorkspace(wsId).then((w) => alive && setWs(w)).catch(() => alive && setWs(null));
    return () => {
      alive = false;
    };
  }, [wsId]);
  return ws;
}

/** Side nav grouped by journey (spec v3 §9), built from the route manifest. */
function JourneyNav({ wsId, wsName }: { wsId?: string; wsName?: string }) {
  return (
    <>
      {wsId && (
        <div className="nav-section">
          <Link to="/" className="nav-back">← All workspaces</Link>
          <div className="nav-ws" title={wsName ?? wsId}>{wsName ?? "Workspace"}</div>
        </div>
      )}
      {JOURNEYS.map((j) => {
        const screens = SCREENS.filter((s) => s.journey === j.id && s.nav && (!s.workspace || wsId) && !(wsId && s.id === "workspaces"));
        if (!screens.length) return null;
        return (
          <div key={j.id} className="nav-journey" role="group" aria-labelledby={`nav-j-${j.id}`}>
            <div id={`nav-j-${j.id}`} className="nav-journey-label" title={j.description}>{j.label}</div>
            <ul className="nav-list">
              {screens.map((s) => (
                <li key={s.id}>
                  <NavLink to={fillPath(s.path, { wsId })} end={s.id === "workspace-home" || s.id === "workspaces" || s.id === "investigations"}
                    className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
                    {s.id === "workspace-home" ? "What changed" : s.title}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </>
  );
}

export function Layout() {
  const { user, logout } = useAuth();
  const wsId = useMatch("/w/:wsId/*")?.params.wsId;
  const ws = useWorkspace(wsId);
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  useEffect(() => setNavOpen(false), [location.pathname]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="shell">
      <a href="#main" className="skip-link">Skip to content</a>
      <header className="topbar">
        <button type="button" className="btn btn-ghost nav-toggle" aria-expanded={navOpen} aria-controls="sidenav"
          onClick={() => setNavOpen((o) => !o)}>
          ☰<span className="sr-only">Toggle navigation</span>
        </button>
        <Link to="/" className="brand">
          <span className="brand-mark" aria-hidden="true">◆</span>
          <span>Context2AI <strong>AnalystOS</strong></span>
        </Link>
        <div className="topbar-spacer" />
        <button type="button" className="btn btn-sm palette-trigger" onClick={() => setPaletteOpen(true)} aria-haspopup="dialog"
          aria-keyshortcuts="Control+K Meta+K">
          <span>Go to…</span> <kbd className="kbd">Ctrl K</kbd>
        </button>
        <ThemeToggle />
        {user && (
          <div className="topbar-user">
            <NotificationBell />
            <span className="user-name" title={user.email}>{user.name || user.email}</span>
            {user.is_admin && <span className="tag tag-info">admin</span>}
            <button type="button" className="btn btn-sm" onClick={logout}>Log out</button>
          </div>
        )}
      </header>
      <div className="body">
        <nav id="sidenav" className={`sidenav ${navOpen ? "open" : ""}`} aria-label="Main">
          <JourneyNav wsId={wsId} wsName={ws?.name} />
        </nav>
        <main id="main" className="main" tabIndex={-1}>
          <ErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} wsId={wsId} />
    </div>
  );
}
