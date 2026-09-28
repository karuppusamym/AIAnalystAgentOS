import { lazy, type ComponentType } from "react";

const KEY = "analystos.chunkReloadAt";
const WINDOW_MS = 60_000;

/**
 * `React.lazy` for a page chunk that survives a redeploy: a tab opened before a new build still asks for the old
 * chunk file names, which no longer exist, and the page stays blank. On such a failure the app reloads once (at most
 * once a minute, remembered for this tab) to pick up the new build; a second failure is shown as the error it is.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function lazyWithReload<T extends ComponentType<any>>(load: () => Promise<{ default: T }>) {
  return lazy(async () => {
    try {
      return await load();
    } catch (err) {
      let last = 0;
      try {
        last = Number(sessionStorage.getItem(KEY) || 0);
      } catch {
        /* storage blocked: reload anyway, the window below still bounds it */
      }
      if (Date.now() - last > WINDOW_MS) {
        try {
          sessionStorage.setItem(KEY, String(Date.now()));
        } catch {
          /* storage blocked */
        }
        window.location.reload();
        return new Promise<never>(() => {}); // the page is reloading
      }
      throw err;
    }
  });
}
