import { useState } from "react";
import { api, type PinStatus } from "../api";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { ErrorBox, Notice, StatusBadge, TechnicalDetails } from "./ui";

const fmt = (v: unknown) => (v === undefined || v === null ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v));

/**
 * A schedule's pins (P7-03): it keeps running the versions it was created with. When newer versions
 * exist it says "upgrade available", shows what would change, and the owner accepts it; the accept is
 * bound to the revision and the upgrade they reviewed, so a later change cannot slip in.
 */
export function SchedulePins({ wsId, scheduleId, canEdit, onUpgraded }: { wsId: string; scheduleId: string; canEdit: boolean; onUpgraded?: () => void }) {
  const detail = useAsync(() => api.getSchedule(wsId, scheduleId), [wsId, scheduleId]);
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const act = useAction();
  const pin: PinStatus | undefined = detail.data?.pin_status;
  if (detail.error) return <p className="muted small">Pinned versions could not be checked: {detail.error}</p>;
  if (!pin || pin.state === "unpinned") return null;
  const changed = pin.items.filter((i) => i.state !== "current");
  const accept = async () => {
    if (!detail.data) return;
    const r = await act.run(() => api.upgradeSchedule(wsId, scheduleId, detail.data!.revision, pin.upgrade_hash));
    if (r) {
      setDone(`Upgraded. The next run uses the new versions${r.added.length ? ` (${r.added.length} changed)` : ""}.`);
      setOpen(false);
      await detail.reload();
      onUpgraded?.();
    }
  };
  return (
    <div className="pins" aria-label="Pinned versions">
      <p className="small">
        {pin.state === "current" && <><StatusBadge status="ok" label="up to date" /> Runs the versions it was set up with; they are current.</>}
        {pin.state === "upgrade_available" && <><StatusBadge status="pending" label="upgrade available" /> Newer versions of what this schedule runs exist.
          It keeps running the pinned versions until someone accepts the upgrade.</>}
        {pin.state === "deprecated" && <><StatusBadge status="pending" label="deprecated" /> Something it runs is deprecated; it still runs.</>}
        {pin.state === "blocked" && <><StatusBadge status="failed" label="blocked" /> It cannot run: {pin.blocking.join("; ")}</>}
      </p>
      {pin.warnings.map((w) => <p key={w} className="small warn-text">{w}</p>)}
      {changed.length > 0 && (
        <button type="button" className="btn btn-xs" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Hide changes" : "What would change"}</button>
      )}
      {open && (
        <div className="stack">
          <ul className="list compact" aria-label="Changes in the upgrade">
            {changed.map((i) => (
              <li key={`${i.type}:${i.id}`}>
                <strong>{i.id}</strong> <span className="muted small">{i.type.replace(/_/g, " ")}</span>: {i.pinned ?? "—"} → {i.current ?? "—"}
                {" "}<StatusBadge status={i.state === "newer" ? "pending" : i.state === "retired" || i.state === "rejected" ? "failed" : "neutral"} label={i.state} />
                {i.reason && <div className="small muted">{i.reason}</div>}
                {i.diff.length > 0 && (
                  <table className="table table-compact" aria-label={`Changes to ${i.id}`}>
                    <thead><tr><th>Field</th><th>Pinned</th><th>Current</th></tr></thead>
                    <tbody>{i.diff.slice(0, 50).map((d) => <tr key={d.path}><td><code>{d.path}</code></td><td className="small">{fmt(d.from)}</td><td className="small">{fmt(d.to)}</td></tr>)}</tbody>
                  </table>
                )}
              </li>
            ))}
          </ul>
          {pin.state === "upgrade_available" && canEdit && (
            <div className="form-actions">
              <button type="button" className="btn btn-sm btn-primary" disabled={act.busy} onClick={() => void accept()}>Accept upgrade</button>
            </div>
          )}
          {pin.state === "upgrade_available" && !canEdit && <p className="muted small">An editor of this workspace accepts upgrades.</p>}
        </div>
      )}
      <ErrorBox error={act.error} />
      {done && <Notice tone="success">{done}</Notice>}
      <TechnicalDetails><p className="small">Pin revision {pin.revision} · checked {fmtDate(pin.checked_at)}{pin.upgrade_hash ? <> · upgrade <code>{pin.upgrade_hash.slice(0, 12)}</code></> : null}</p></TechnicalDetails>
    </div>
  );
}
