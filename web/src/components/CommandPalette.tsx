import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Run, type Workspace } from "../api";
import { fillPath, JOURNEYS, SCREENS, to } from "../routes";

export interface PaletteItem {
  id: string;
  label: string;
  group: string;
  hint?: string;
  href: string;
  keywords?: string;
}

const JOURNEY_LABEL = Object.fromEntries(JOURNEYS.map((j) => [j.id, j.label])) as Record<string, string>;

/** Navigable screens for the current context: global screens always, workspace screens when one is open. */
export function screenItems(wsId: string | undefined, wsName?: string): PaletteItem[] {
  return SCREENS.filter((s) => s.nav && (!s.workspace || wsId)).map((s) => ({
    id: `screen:${s.id}`,
    label: s.title,
    group: JOURNEY_LABEL[s.journey] ?? "Go to",
    hint: s.workspace ? wsName ?? "this workspace" : undefined,
    href: fillPath(s.path, { wsId }),
    keywords: `${s.journey} ${s.keywords ?? ""}`,
  }));
}

/** Case-insensitive match on every whitespace-separated term, against label, group and keywords. */
export function filterItems(items: PaletteItem[], query: string): PaletteItem[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return items;
  return items.filter((it) => {
    const hay = `${it.label} ${it.group} ${it.hint ?? ""} ${it.keywords ?? ""}`.toLowerCase();
    return terms.every((t) => hay.includes(t));
  });
}

/**
 * Ctrl/Cmd-K: jump to any screen, workspace or recent run. A modal dialog with a combobox and a
 * listbox: arrow keys move, Enter opens, Esc closes and returns focus to where it was, and Tab
 * stays inside the dialog.
 */
export function CommandPalette({ open, onClose, wsId }: { open: boolean; onClose: () => void; wsId?: string }) {
  const nav = useNavigate();
  const titleId = useId();
  const listId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);

  useEffect(() => {
    if (!open) return;
    returnFocus.current = document.activeElement as HTMLElement | null;
    setQuery("");
    setActive(0);
    inputRef.current?.focus();
    let alive = true;
    api.listWorkspaces().then((w) => alive && setWorkspaces(w)).catch(() => undefined);
    if (wsId) api.listRuns(wsId).then((r) => alive && setRuns(r.slice(0, 8))).catch(() => undefined);
    else setRuns([]);
    return () => {
      alive = false;
      returnFocus.current?.focus?.();
    };
  }, [open, wsId]);

  const items = useMemo(() => {
    const ws = workspaces.find((w) => w.id === wsId);
    return [
      ...screenItems(wsId, ws?.name),
      ...workspaces.map((w) => ({ id: `ws:${w.id}`, label: w.name, group: "Workspaces", href: to.workspace(w.id), keywords: "workspace" })),
      ...(wsId ? runs.map((r) => ({
        id: `run:${r.id}`, label: r.objective || r.id, group: "Recent investigations", hint: r.status,
        href: to.run(wsId, r.id), keywords: `run ${r.id}`,
      })) : []),
    ];
  }, [workspaces, runs, wsId]);
  const shown = useMemo(() => filterItems(items, query), [items, query]);
  useEffect(() => setActive((a) => Math.min(a, Math.max(0, shown.length - 1))), [shown.length]);

  if (!open) return null;

  const go = (it: PaletteItem | undefined) => {
    if (!it) return;
    onClose();
    nav(it.href);
  };

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => (shown.length ? (a + 1) % shown.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (shown.length ? (a - 1 + shown.length) % shown.length : 0));
    } else if (e.key === "Enter" && e.target === inputRef.current) {
      e.preventDefault();
      go(shown[active]);
    } else if (e.key === "Tab") {
      // Focus trap: cycle among the dialog's own focusable elements.
      const focusables = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("input, button, [href], [tabindex]:not([tabindex='-1'])") ?? []);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };

  let lastGroup = "";
  return (
    <div className="palette-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={dialogRef} className="palette" role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={onKeyDown}>
        <h2 id={titleId} className="sr-only">Go to a screen, workspace or run</h2>
        <div className="palette-head">
          <input ref={inputRef} className="palette-input" type="text" role="combobox" aria-expanded="true" aria-controls={listId}
            aria-autocomplete="list" aria-label="Search screens, workspaces and runs" placeholder="Go to…"
            aria-activedescendant={shown[active] ? `${listId}-${active}` : undefined}
            value={query} onChange={(e) => { setQuery(e.target.value); setActive(0); }} />
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>Esc<span className="sr-only"> close</span></button>
        </div>
        <ul id={listId} role="listbox" className="palette-list" aria-label="Results">
          {shown.length === 0 && <li className="palette-empty" role="presentation">No matches</li>}
          {shown.map((it, i) => {
            const header = it.group !== lastGroup ? it.group : null;
            lastGroup = it.group;
            return (
              <li key={it.id} id={`${listId}-${i}`} role="option" aria-selected={i === active}
                className={`palette-item ${i === active ? "active" : ""}`}
                onMouseEnter={() => setActive(i)} onClick={() => go(it)}>
                {header && <span className="palette-group" aria-hidden="true">{header}</span>}
                <span className="palette-label">{it.label}</span>
                <span className="sr-only">, {it.group}</span>
                {it.hint && <span className="palette-hint">{it.hint}</span>}
              </li>
            );
          })}
        </ul>
        <p className="palette-foot muted small" aria-hidden="true">↑↓ to move · Enter to open · Esc to close</p>
      </div>
    </div>
  );
}
