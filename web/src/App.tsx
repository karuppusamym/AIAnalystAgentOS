import { lazy, Suspense, type ReactElement, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { EmptyState, Loading } from "./components/ui";
import { AdminPage } from "./pages/Admin";
import { ApprovalsPage } from "./pages/Approvals";
import { AskPage } from "./pages/Ask";
import { ConsolePage } from "./pages/Console";
import { GovernancePage } from "./pages/Governance";
import { InsightsPage } from "./pages/Insights";
import { LoginPage } from "./pages/Login";
import { MonitoringPage } from "./pages/Monitoring";
import { ReportsPage } from "./pages/Reports";
import { RunsPage } from "./pages/Runs";
import { RunViewPage } from "./pages/RunView";
import { SchedulesPage } from "./pages/Schedules";
import { SourcesPage } from "./pages/Sources";
import { StudioPage } from "./pages/Studio";
import { WorkspaceHomePage } from "./pages/WorkspaceHome";
import { WorkspacesPage } from "./pages/Workspaces";
import { fillPath, LEGACY_REDIRECTS, SCREENS, type ScreenId } from "./routes";

// The knowledge studio (catalog, documents, review queue, graph, import/export) loads on first visit.
const CatalogPage = lazy(() => import("./pages/Catalog").then((m) => ({ default: m.CatalogPage })));

function RequireAuth({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const location = useLocation();
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return <>{children}</>;
}

/** One element per screen in the manifest: a screen without an element is a type error. */
const ELEMENTS: Record<ScreenId, ReactElement> = {
  login: <LoginPage />,
  workspaces: <WorkspacesPage />,
  "workspace-home": <WorkspaceHomePage />,
  ask: <AskPage />,
  investigations: <RunsPage />,
  investigation: <RunViewPage />,
  "investigation-console": <ConsolePage />,
  findings: <InsightsPage />,
  sources: <SourcesPage />,
  catalog: <Suspense fallback={<div className="page"><Loading /></div>}><CatalogPage /></Suspense>,
  studio: <StudioPage />,
  reports: <ReportsPage />,
  approvals: <ApprovalsPage />,
  monitoring: <MonitoringPage />,
  schedules: <SchedulesPage />,
  governance: <GovernancePage />,
  registry: <AdminPage key="registry" section="registry" />,
  settings: <AdminPage key="settings" section="settings" />,
  usage: <AdminPage key="usage" section="usage" />,
};

/** Old URL → new screen, keeping path parameters and the query string (?tab=, ?artifact=, …). */
function LegacyRedirect({ target }: { target: string }) {
  const params = useParams();
  const { search, hash } = useLocation();
  return <Navigate replace to={`${fillPath(target, params as Record<string, string>)}${search}${hash}`} />;
}

export function AppRoutes() {
  const screens = SCREENS.filter((s) => s.id !== "login");
  return (
    <Routes>
      <Route path="/login" element={ELEMENTS.login} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        {screens.map((s) => <Route key={s.id} path={s.path} element={ELEMENTS[s.id]} />)}
        {LEGACY_REDIRECTS.map((r) => <Route key={r.from} path={r.from} element={<LegacyRedirect target={r.to} />} />)}
        <Route path="*" element={<div className="page"><EmptyState title="Page not found" /></div>} />
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}
