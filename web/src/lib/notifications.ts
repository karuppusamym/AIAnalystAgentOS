import type { AppNotification } from "../api";

/**
 * Where a notification's link {type, id} leads in the UI:
 * alert -> monitoring (alerts tab), artifact -> reports (report notifications) or studio,
 * run -> run view, schedule -> schedules. Returns null when there is nowhere sensible to go.
 */
export function notificationHref(n: Pick<AppNotification, "workspace_id" | "kind" | "link">): string | null {
  const ws = n.workspace_id ? `/w/${encodeURIComponent(n.workspace_id)}` : null;
  const type = n.link?.type;
  const id = n.link?.id ? encodeURIComponent(n.link.id) : null;
  if (!ws) return null;
  switch (type) {
    case "alert":
      return `${ws}/monitoring?tab=alerts${id ? `&alert=${id}` : ""}`;
    case "artifact":
      if (!id) return `${ws}/reports`;
      return n.kind === "report" ? `${ws}/reports?artifact=${id}` : `${ws}/studio?artifact=${id}`;
    case "run":
      return id ? `${ws}/runs/${id}` : `${ws}/runs`;
    case "schedule":
      return `${ws}/schedules${id ? `?schedule=${id}` : ""}`;
    default:
      return ws;
  }
}

export function unreadCount(list: Pick<AppNotification, "read">[]): number {
  return list.filter((n) => !n.read).length;
}
