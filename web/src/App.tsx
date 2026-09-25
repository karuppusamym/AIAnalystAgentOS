import type { ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { EmptyState } from "./components/ui";
import { AdminPage } from "./pages/Admin";
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

function RequireAuth({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const location = useLocation();
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return <>{children}</>;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<WorkspacesPage />} />
        <Route path="admin" element={<AdminPage />} />
        <Route path="w/:wsId">
          <Route index element={<WorkspaceHomePage />} />
          <Route path="sources" element={<SourcesPage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:runId" element={<RunViewPage />} />
          <Route path="runs/:runId/console" element={<ConsolePage />} />
          <Route path="insights" element={<InsightsPage />} />
          <Route path="insights/:insightId" element={<InsightsPage />} />
          <Route path="studio" element={<StudioPage />} />
          <Route path="schedules" element={<SchedulesPage />} />
          <Route path="monitoring" element={<MonitoringPage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route path="ask" element={<AskPage />} />
          <Route path="governance" element={<GovernancePage />} />
        </Route>
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
