import type { AppNotification } from "../api";
import { to } from "../routes";

/**
 * Where a notification's link {type, id} leads in the UI:
 * alert -> Operate monitoring (alerts tab), artifact -> Build reports (report notifications) or
 * studio, run -> Investigate board, schedule -> Operate schedules. Returns null when there is
 * nowhere sensible to go.
 */
export function notificationHref(n: Pick<AppNotification, "workspace_id" | "kind" | "link">): string | null {
  const ws = n.workspace_id;
  const type = n.link?.type;
  const id = n.link?.id || undefined;
  if (!ws) return null;
  switch (type) {
    case "alert":
      return to.monitoring(ws, { tab: "alerts", alert: id });
    case "artifact":
      if (!id) return to.reports(ws);
      return n.kind === "report" ? to.reports(ws, id) : to.studio(ws, id);
    case "run":
      return id ? to.run(ws, id) : to.investigations(ws);
    case "schedule":
      return to.schedules(ws, id);
    default:
      return to.workspace(ws);
  }
}

export function unreadCount(list: Pick<AppNotification, "read">[]): number {
  return list.filter((n) => !n.read).length;
}
