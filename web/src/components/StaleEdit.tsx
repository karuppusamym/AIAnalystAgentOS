import { useState } from "react";
import { ApiError, errorMessage } from "../api";
import { diffJson, preview } from "../lib/diff";
import { ErrorBox, Tag } from "./ui";

/**
 * A 412 (someone saved a newer revision since this was opened) or 428 (the edit carried no revision)
 * from an `If-Match` edit. Nothing was overwritten; the person reloads, or compares first.
 */
export function isStaleEdit(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 412 || err.status === 428);
}

/**
 * "Someone changed this": shown instead of the raw error when an edit is refused as stale. Compare
 * fetches the current version and lists, path by path, how it differs from what this person had
 * open (their unsaved change stays in the form, so nothing typed is lost).
 */
export function StaleEditNotice({ what, mine, loadCurrent, onReload }: {
  what: string;
  /** What this person was editing against (the version they had open, with their change). */
  mine: unknown;
  loadCurrent: () => Promise<unknown>;
  onReload: () => void;
}) {
  const [changes, setChanges] = useState<ReturnType<typeof diffJson> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const compare = async () => {
    setBusy(true);
    setError(null);
    try {
      setChanges(diffJson(mine, await loadCurrent()));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="alert alert-warning stale-edit" role="alert">
      <p><strong>Someone changed this.</strong> {what} was saved by someone else after you opened it, so your change was not applied
        and nothing was overwritten. Reload to see the current version, or compare it with yours first.</p>
      <div className="chip-row">
        <button type="button" className="btn btn-sm btn-primary" onClick={onReload}>Reload the current version</button>
        <button type="button" className="btn btn-sm" onClick={() => void compare()} disabled={busy} aria-expanded={changes !== null}>
          {busy ? "Comparing…" : "Compare with mine"}</button>
      </div>
      <ErrorBox error={error} />
      {changes && changes.length === 0 && <p className="small">The current version has the same content as yours.</p>}
      {changes && changes.length > 0 && (
        <div className="table-wrap" tabIndex={0}>
          <table className="table table-compact diff-table">
            <caption className="sr-only">Differences between your version and the current one</caption>
            <thead><tr><th scope="col">Path</th><th scope="col">Change</th><th scope="col">Yours</th><th scope="col">Current</th></tr></thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.path} className={`diff-${c.kind}`}>
                  <td><code>{c.path || "(whole value)"}</code></td>
                  <td><Tag tone={c.kind === "added" ? "success" : c.kind === "removed" ? "danger" : "warning"}>{c.kind}</Tag></td>
                  <td className="small diff-before">{preview(c.before)}</td>
                  <td className="small diff-after">{preview(c.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
