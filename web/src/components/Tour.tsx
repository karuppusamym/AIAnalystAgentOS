import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { markTourDone, tour as findTour, type TourId, type TourStep } from "../lib/guide";

interface TourApi {
  start: (id: TourId, wsId: string) => void;
  active: TourId | null;
}

const TourContext = createContext<TourApi>({ start: () => undefined, active: null });

export function useTour(): TourApi {
  return useContext(TourContext);
}

/** How long a step waits for its target to render (a screen's data loading) before showing centred. */
let targetWaitMs = 2500;

/** Tests have no layout (every rect is empty), so they shorten the wait. */
export function setTourTargetWait(ms: number): void {
  targetWaitMs = ms;
}

function visibleRect(el: Element | null): DOMRect | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 ? r : null;
}

/**
 * Plays a guided tour over the real screens (lib/guide.ts). A step may move to another screen and
 * point at a `data-tour` anchor there; when the anchor is not on screen (a narrow layout hides the
 * nav, a list is still empty) the step shows as a centred card rather than failing. The tour never
 * clicks or changes anything: it only navigates and highlights.
 */
export function TourProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<{ id: TourId; ws: string; index: number } | null>(null);
  const start = useCallback((id: TourId, wsId: string) => setState({ id, ws: wsId, index: 0 }), []);
  const api = useMemo(() => ({ start, active: state?.id ?? null }), [start, state?.id]);
  return (
    <TourContext.Provider value={api}>
      {children}
      {state && (
        <TourOverlay key={`${state.id}:${state.index}`} tourId={state.id} ws={state.ws} index={state.index}
          onMove={(index) => setState((s) => s && { ...s, index })}
          onEnd={(completed) => {
            if (completed) markTourDone(state.id);
            setState(null);
          }} />
      )}
    </TourContext.Provider>
  );
}

function TourOverlay({ tourId, ws, index, onMove, onEnd }: {
  tourId: TourId; ws: string; index: number; onMove: (i: number) => void; onEnd: (completed: boolean) => void;
}) {
  const t = findTour(tourId);
  const step: TourStep = t.steps[index];
  const last = index === t.steps.length - 1;
  const navigate = useNavigate();
  const location = useLocation();
  const [rect, setRect] = useState<DOMRect | null>(null);
  const [searching, setSearching] = useState(!!step.target);
  const cardRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  const target = step.path?.(ws);
  const here = `${location.pathname}${location.search}`;
  useEffect(() => {
    if (target && target !== here) navigate(target);
    // navigate once per step; `here` changing after the navigation must not re-trigger it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  useEffect(() => {
    if (!step.target) {
      setSearching(false);
      return;
    }
    const began = Date.now();
    let frame = 0;
    const look = () => {
      const el = document.querySelector(step.target!);
      const r = visibleRect(el);
      if (r) {
        el!.scrollIntoView({ block: "center", inline: "nearest" });
        setRect(visibleRect(el));
        setSearching(false);
        return;
      }
      if (Date.now() - began > targetWaitMs) {
        setSearching(false);
        return;
      }
      frame = window.setTimeout(look, 100);
    };
    look();
    return () => window.clearTimeout(frame);
  }, [step.target, location.pathname, location.search]);

  useEffect(() => {
    if (!step.target || !rect) return;
    const update = () => setRect(visibleRect(document.querySelector(step.target!)));
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [step.target, rect !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!searching) cardRef.current?.focus();
  }, [searching]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onEnd(false);
      } else if (e.key === "ArrowRight" && cardRef.current?.contains(document.activeElement)) {
        e.preventDefault();
        if (last) onEnd(true);
        else onMove(index + 1);
      } else if (e.key === "ArrowLeft" && index > 0 && cardRef.current?.contains(document.activeElement)) {
        e.preventDefault();
        onMove(index - 1);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [index, last, onEnd, onMove]);

  useLayoutEffect(() => {
    document.body.classList.add("tour-running");
    return () => document.body.classList.remove("tour-running");
  }, []);

  const pad = 6;
  const cardStyle = placeCard(rect);
  return (
    <div className="tour-layer" aria-live="polite">
      {rect ? (
        <div className="tour-spotlight" aria-hidden="true"
          style={{ top: rect.top - pad, left: rect.left - pad, width: rect.width + pad * 2, height: rect.height + pad * 2 }} />
      ) : (
        <div className="tour-dim" aria-hidden="true" />
      )}
      {!searching && (
        <div ref={cardRef} className={`tour-card ${rect ? "" : "tour-card-centred"}`} role="dialog" aria-modal="false"
          aria-labelledby={titleId} tabIndex={-1} style={cardStyle}>
          <p className="tour-progress">{t.title} · step {index + 1} of {t.steps.length}</p>
          <h2 id={titleId} className="tour-title">{step.title}</h2>
          <p className="tour-body">{step.body}</p>
          {step.target && !rect && (
            <p className="tour-note">This part is not on screen right now (the list may be empty, or the menu is folded at this width).</p>
          )}
          <div className="tour-actions">
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => onEnd(false)}>End tour</button>
            <span className="tour-spacer" />
            {index > 0 && <button type="button" className="btn btn-sm" onClick={() => onMove(index - 1)}>Back</button>}
            <button type="button" className="btn btn-sm btn-primary" onClick={() => (last ? onEnd(true) : onMove(index + 1))}>
              {last ? "Finish" : "Next"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

const CARD_W = 360;

/** Below the target when it fits, else above, else beside; always inside the viewport. */
export function placeCard(rect: DOMRect | null, vw = window.innerWidth, vh = window.innerHeight): React.CSSProperties {
  if (!rect) return {};
  const width = Math.min(CARD_W, vw - 24);
  const left = Math.max(12, Math.min(rect.left, vw - width - 12));
  const room = 220;
  if (rect.bottom + room < vh) return { top: rect.bottom + 14, left, width };
  if (rect.top - room > 0) return { top: Math.max(12, rect.top - room - 14), left, width };
  const beside = rect.right + width + 24 < vw ? rect.right + 14 : 12;
  return { top: Math.max(12, Math.min(rect.top, vh - room - 12)), left: beside, width };
}
