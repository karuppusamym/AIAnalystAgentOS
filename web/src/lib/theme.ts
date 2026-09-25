/**
 * Colour theme: "system" follows prefers-color-scheme; "light"/"dark" pin it. The choice is set
 * as `data-theme` on <html> (styles.css keys the dark tokens off it) and remembered in
 * localStorage, which may be unavailable (private mode, blocked storage): then it lasts the tab.
 */
import { useEffect, useSyncExternalStore } from "react";

export type ThemePref = "system" | "light" | "dark";
const KEY = "analystos.theme";
const QUERY = "(prefers-color-scheme: dark)";

function readStored(): ThemePref {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

let pref: ThemePref = typeof window === "undefined" ? "system" : readStored();
const listeners = new Set<() => void>();

function systemDark(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(QUERY).matches;
}

export function resolveTheme(p: ThemePref, sysDark: boolean): "light" | "dark" {
  return p === "system" ? (sysDark ? "dark" : "light") : p;
}

function apply(): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (pref === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", pref);
}

export function getThemePref(): ThemePref {
  return pref;
}

export function setThemePref(next: ThemePref): void {
  pref = next;
  try {
    if (next === "system") window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, next);
  } catch {
    /* storage unavailable: keep the choice for this tab only */
  }
  apply();
  listeners.forEach((l) => l());
}

/** Cycle system → light → dark → system (the toggle's order). */
export function nextThemePref(p: ThemePref): ThemePref {
  return p === "system" ? "light" : p === "light" ? "dark" : "system";
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  let mq: MediaQueryList | null = null;
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    mq = window.matchMedia(QUERY);
    mq.addEventListener?.("change", cb);
  }
  return () => {
    listeners.delete(cb);
    mq?.removeEventListener?.("change", cb);
  };
}

/** The user's preference and the theme actually in effect (tracks OS changes under "system"). */
export function useTheme(): { pref: ThemePref; resolved: "light" | "dark" } {
  const snapshot = useSyncExternalStore(subscribe, () => `${pref}|${systemDark() ? 1 : 0}`, () => "system|0");
  const [p, d] = snapshot.split("|");
  useEffect(apply, [snapshot]);
  return { pref: p as ThemePref, resolved: resolveTheme(p as ThemePref, d === "1") };
}

apply();
