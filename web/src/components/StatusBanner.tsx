import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Health, type HealthProblem } from "../api";

export const HEALTH_POLL_MS = 30_000;
export const HEALTH_TIMEOUT_MS = 8_000;

/** What to tell people, from one health probe: the API itself, then critical and warning problems it reports. */
export function bannerProblems(result: { health?: Health | null; error?: string | null }): HealthProblem[] {
  if (result.error) {
    return [{ dependency: "api", severity: "critical", detail: result.error,
      message: "The AnalystOS API is not responding: pages cannot load or save until it is back. Checking again every 30 seconds." }];
  }
  const health = result.health;
  if (!health || typeof health !== "object" || Array.isArray(health)) return [];
  if (Array.isArray(health.problems)) return health.problems.filter((p) => p.severity !== "info");
  // An API without `problems`: fall back to the checks the demo path depends on.
  const named: Record<string, string> = {
    temporal: "Temporal is unreachable: new runs cannot start.",
    worker: "No worker is running: runs stay queued.",
    superset: "Superset is not reachable: publications go to the in-platform preview.",
  };
  return Object.entries(named)
    .filter(([k]) => health.checks?.[k] && health.checks[k].ok === false)
    .map(([k, message]) => ({ dependency: k, severity: k === "superset" ? "warning" : "critical", message,
      detail: health.checks?.[k]?.error ?? null }));
}

/**
 * A banner under the top bar when the API, Temporal, a worker or Superset is down, so a stalled page
 * says why instead of spinning. Polls /api/health; a probe that takes longer than HEALTH_TIMEOUT_MS
 * counts as "not responding".
 */
export function StatusBanner() {
  const [problems, setProblems] = useState<HealthProblem[]>([]);
  const [checking, setChecking] = useState(false);
  const alive = useRef(true);

  const probe = useCallback(async () => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), HEALTH_TIMEOUT_MS);
    setChecking(true);
    let next: HealthProblem[];
    try {
      next = bannerProblems({ health: await api.health(ctrl.signal) });
    } catch (err) {
      const message = ctrl.signal.aborted ? `no answer within ${HEALTH_TIMEOUT_MS / 1000} s`
        : err instanceof Error ? err.message : String(err);
      next = bannerProblems({ error: message });
    } finally {
      clearTimeout(timer);
    }
    if (alive.current) {
      setProblems(next);
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void probe();
    const id = setInterval(() => void probe(), HEALTH_POLL_MS);
    return () => {
      alive.current = false;
      clearInterval(id);
    };
  }, [probe]);

  if (!problems.length) return null;
  const critical = problems.some((p) => p.severity === "critical");
  return (
    <div className={`status-banner status-banner-${critical ? "critical" : "warning"}`} role={critical ? "alert" : "status"}
      aria-label="Service status">
      <ul className="status-banner-list">
        {problems.map((p) => (
          <li key={p.dependency}>
            <strong>{p.dependency === "api" ? "API" : p.dependency[0].toUpperCase() + p.dependency.slice(1)}:</strong> {p.message}
            {p.detail && <span className="status-banner-detail"> ({p.detail})</span>}
          </li>
        ))}
      </ul>
      <button type="button" className="btn btn-sm" onClick={() => void probe()} disabled={checking}>
        {checking ? "Checking…" : "Check again"}
      </button>
    </div>
  );
}
