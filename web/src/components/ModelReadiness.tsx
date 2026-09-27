import { api } from "../api";
import { useAsync } from "../lib/hooks";
import { ErrorBox, Notice } from "./ui";

/** Read-only readiness in the API process. Historical usage is reported separately. */
export function ModelReadiness() {
  const health = useAsync(() => api.modelHealth(), []);
  const providers = health.data?.providers ?? [];
  const missing = providers.filter((p) => p.key_required && !p.key_present);
  const blocked = providers.filter((p) => p.cooldown);
  if (health.error) return <ErrorBox error={health.error} onRetry={health.reload} />;
  if (!health.data) return <p className="muted small" role="status">Checking model connection…</p>;
  return <div className="model-readiness">
    {missing.length > 0 && <Notice tone="warning">
      <strong>{missing.length === providers.length ? "No model provider is connected." : "Some model providers need configuration."}</strong>{" "}
      Rule-based analysis can still produce findings. Choosing “always” does not connect a provider.
      <div className="small">Set {Array.from(new Set(missing.map((p) => p.api_key_env))).join(", ")} in the API and worker environment, then recreate those services.
        {" "}<a href="/settings/platform?tab=models">View provider status</a></div>
    </Notice>}
    {blocked.length > 0 && <Notice tone="warning">Provider calls are temporarily paused: {blocked.map((p) => `${p.provider}: ${p.cooldown?.reason}`).join("; ")}</Notice>}
    {!health.data.counters_available && <Notice tone="warning">Spend counters are unavailable. Billable model calls are paused until the counter store recovers.</Notice>}
    {!missing.length && !blocked.length && health.data.counters_available && <p className="muted small">Provider credentials are configured in the API. Call history below shows actual usage; credentials alone do not prove a successful connection.</p>}
  </div>;
}
