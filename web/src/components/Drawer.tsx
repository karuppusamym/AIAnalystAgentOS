import { useEffect, useId, useRef, type ReactNode } from "react";

/**
 * A modal side drawer: focus moves to Close on open, Tab stays inside, Esc and the backdrop close
 * it, and the caller returns focus to the control that opened it (`onClose`).
 */
export function Drawer({ title, onClose, children, className = "" }: { title: ReactNode; onClose: () => void; children: ReactNode; className?: string }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const f = panelRef.current.querySelectorAll<HTMLElement>(
        "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])");
      if (!f.length) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className={`drawer ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panelRef}>
        <header className="drawer-head">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="btn btn-sm btn-ghost" ref={closeRef} onClick={onClose}>Close</button>
        </header>
        <div className="drawer-body">{children}</div>
      </div>
    </div>
  );
}
