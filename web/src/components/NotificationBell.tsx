import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage, type AppNotification } from "../api";
import { fmtDate } from "../lib/format";
import { notificationHref } from "../lib/notifications";

export const NOTIFICATION_POLL_MS = 30_000;

/**
 * Top-bar bell: polls GET /api/notifications?unread=true every 30 s for the badge; opening the
 * dropdown loads the full list. Clicking an item marks it read and follows its link.
 */
export function NotificationBell({ pollMs = NOTIFICATION_POLL_MS }: { pollMs?: number }) {
  const [unread, setUnread] = useState<AppNotification[]>([]);
  const [all, setAll] = useState<AppNotification[] | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  const poll = useCallback(async () => {
    try {
      setUnread(await api.notifications(true));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  const loadAll = useCallback(async () => {
    try {
      setAll(await api.notifications(false));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void poll();
    const t = setInterval(() => void poll(), pollMs);
    return () => clearInterval(t);
  }, [poll, pollMs]);

  useEffect(() => {
    if (!open) return;
    void loadAll();
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, loadAll]);

  const markRead = async (ids: number[]) => {
    if (!ids.length) return;
    setBusy(true);
    try {
      await api.markNotificationsRead(ids);
      const gone = new Set(ids);
      setUnread((prev) => prev.filter((n) => !gone.has(n.id)));
      setAll((prev) => prev?.map((n) => (gone.has(n.id) ? { ...n, read: true } : n)) ?? prev);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const follow = async (n: AppNotification) => {
    if (!n.read) await markRead([n.id]);
    const href = notificationHref(n);
    setOpen(false);
    if (href) navigate(href);
  };

  const count = unread.length;
  const list = all ?? unread;
  return (
    <div className="bell" ref={rootRef}>
      <button type="button" className="btn btn-ghost btn-sm bell-button" aria-haspopup="true" aria-expanded={open}
        aria-label={count ? `Notifications, ${count} unread` : "Notifications"} onClick={() => setOpen((o) => !o)}>
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round">
          <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
          <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
        </svg>
        {count > 0 && <span className="bell-count" data-testid="bell-count">{count > 99 ? "99+" : count}</span>}
      </button>
      {open && (
        <div className="bell-menu card" role="dialog" aria-label="Notifications">
          <div className="bell-head">
            <strong>Notifications</strong>
            <button type="button" className="btn btn-xs btn-ghost" disabled={busy || count === 0}
              onClick={() => void markRead(unread.map((n) => n.id))}>Mark all read</button>
          </div>
          {error && <p className="small warn-text" role="alert">{error}</p>}
          {list.length === 0 ? <p className="muted small bell-empty">{all === null ? "Loading…" : "No notifications."}</p> : (
            <ul className="bell-list">
              {list.map((n) => (
                <li key={n.id} className={`bell-item ${n.read ? "" : "unread"}`}>
                  <button type="button" className="bell-link" onClick={() => void follow(n)}>
                    <span className="bell-title">{n.title}</span>
                    {n.body && <span className="small muted clamp-2">{n.body}</span>}
                    <span className="small muted">{n.kind} · {fmtDate(n.created_at)}</span>
                  </button>
                  {!n.read && (
                    <button type="button" className="btn btn-xs btn-ghost" disabled={busy} onClick={() => void markRead([n.id])}
                      aria-label={`Mark “${n.title}” read`}>Mark read</button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
