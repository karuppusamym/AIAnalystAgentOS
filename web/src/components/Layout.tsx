import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useMatch } from "react-router-dom";
import { api, type Workspace } from "../api";
import { useAuth } from "../auth";
import { ErrorBoundary } from "./ErrorBoundary";
import { NotificationBell } from "./NotificationBell";

function WorkspaceNav({ wsId }: { wsId: string }) {
  const [ws, setWs] = useState<Workspace | null>(null);
  useEffect(() => {
    let alive = true;
    api.getWorkspace(wsId).then((w) => alive && setWs(w)).catch(() => alive && setWs(null));
    return () => {
      alive = false;
    };
  }, [wsId]);
  const base = `/w/${encodeURIComponent(wsId)}`;
  const items: [string, string, boolean?][] = [
    [base, "Home", true],
    [`${base}/sources`, "Sources & data"],
    [`${base}/catalog`, "Catalog"],
    [`${base}/runs`, "Analysis runs"],
    [`${base}/insights`, "Insights"],
    [`${base}/studio`, "Studio"],
    [`${base}/schedules`, "Schedules"],
    [`${base}/monitoring`, "Monitoring"],
    [`${base}/reports`, "Reports"],
    [`${base}/ask`, "Ask (SQL)"],
    [`${base}/governance`, "Policy & members"],
  ];
  return (
    <>
      <div className="nav-section">
        <Link to="/" className="nav-back">← All workspaces</Link>
        <div className="nav-ws" title={ws?.name ?? wsId}>{ws?.name ?? "Workspace"}</div>
      </div>
      <ul className="nav-list">
        {items.map(([to, label, end]) => (
          <li key={to}>
            <NavLink to={to} end={end} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
              {label}
            </NavLink>
          </li>
        ))}
      </ul>
    </>
  );
}

export function Layout() {
  const { user, logout } = useAuth();
  const wsId = useMatch("/w/:wsId/*")?.params.wsId;
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  useEffect(() => setNavOpen(false), [location.pathname]);

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
          {wsId ? <WorkspaceNav wsId={wsId} /> : (
            <ul className="nav-list">
              <li><NavLink to="/" end className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>Workspaces</NavLink></li>
            </ul>
          )}
          <div className="nav-section nav-bottom">
            <ul className="nav-list">
              <li><NavLink to="/admin" className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>Admin &amp; registry</NavLink></li>
            </ul>
          </div>
        </nav>
        <main id="main" className="main" tabIndex={-1}>
          <ErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
