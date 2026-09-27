import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth";
import { CAPABILITIES, DISCIPLINES, TERMS, TOURS, dismissWelcome, toursDone, welcomeDismissed, type TourId } from "../lib/guide";
import { Drawer } from "./Drawer";
import { useTour } from "./Tour";
import { Tabs } from "./ui";

type GuideTab = "can" | "tours" | "terms";

/**
 * The Guide (top bar): what AnalystOS can do, grouped by the job a person has, the guided tours and
 * the product's words. A drawer, not a screen, so the screen budget (routes.ts) is unchanged.
 */
export function GuideButton({ wsId }: { wsId?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button type="button" ref={ref} className="btn btn-sm guide-trigger" data-tour="guide" aria-haspopup="dialog"
        aria-label="Guide" onClick={() => setOpen(true)}>
        <span aria-hidden="true" className="guide-mark">?</span><span className="guide-label" aria-hidden="true"> Guide</span>
      </button>
      {open && <GuideDrawer wsId={wsId} onClose={() => { setOpen(false); ref.current?.focus(); }} />}
    </>
  );
}

export function GuideDrawer({ wsId, onClose, initialTab = "can" }: { wsId?: string; onClose: () => void; initialTab?: GuideTab }) {
  const [tab, setTab] = useState<GuideTab>(initialTab);
  const { user } = useAuth();
  const tourApi = useTour();
  const done = new Set(toursDone());
  const startTour = (id: TourId) => {
    if (!wsId) return;
    onClose();
    tourApi.start(id, wsId);
  };
  const caps = CAPABILITIES.filter((c) => !c.adminOnly || user?.is_admin);
  return (
    <Drawer title="Guide" onClose={onClose} className="guide-drawer">
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "can", label: "What you can do" }, { id: "tours", label: "Tours" }, { id: "terms", label: "Words we use" },
      ]} />
      {tab === "can" && (
        <div className="guide-map">
          {!wsId && <p className="guide-hint">Open a workspace to jump straight to each part.</p>}
          {DISCIPLINES.map((d) => {
            const items = caps.filter((c) => c.discipline === d.id);
            if (!items.length) return null;
            return (
              <section key={d.id} className="guide-group" aria-labelledby={`guide-${d.id}`}>
                <h3 id={`guide-${d.id}`} className="guide-group-title">{d.label}</h3>
                <p className="guide-group-blurb">{d.blurb}</p>
                <ul className="guide-list">
                  {items.map((c) => {
                    const reachable = !c.needsWorkspace || !!wsId;
                    return (
                      <li key={c.id} className="guide-item">
                        <div className="guide-item-text">
                          <strong>{c.title}</strong>
                          <span>{c.what}</span>
                          <span className="guide-who">{c.who}</span>
                        </div>
                        <div className="guide-item-actions">
                          {reachable && <Link className="btn btn-sm" to={c.href(wsId ?? "")} onClick={onClose}>Open</Link>}
                          {c.tour && wsId && (
                            <button type="button" className="btn btn-sm btn-ghost" onClick={() => startTour(c.tour!)}>Show me</button>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}
        </div>
      )}
      {tab === "tours" && (
        <div className="guide-tours">
          {!wsId && <p className="guide-hint">Tours run inside a workspace. Open one (the demo workspace is a good start), then come back here.</p>}
          <ul className="guide-list">
            {TOURS.map((t) => (
              <li key={t.id} className="guide-item">
                <div className="guide-item-text">
                  <strong>{t.title}{done.has(t.id) && <span className="tag tag-success guide-done">taken</span>}</strong>
                  <span>{t.summary}</span>
                  <span className="guide-who">About {t.minutes} min · {t.steps.length} steps</span>
                </div>
                <div className="guide-item-actions">
                  <button type="button" className="btn btn-sm btn-primary" disabled={!wsId} onClick={() => startTour(t.id)}>
                    {done.has(t.id) ? "Take again" : "Start"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
      {tab === "terms" && (
        <dl className="guide-terms">
          {TERMS.map((t) => (
            <div key={t.term} className="guide-term">
              <dt>{t.term}</dt>
              <dd>{t.meaning}</dd>
            </div>
          ))}
        </dl>
      )}
    </Drawer>
  );
}

/**
 * First visit to a workspace: a quiet card on the Overview offering the tour and the map. It is not a
 * modal, so it never blocks the first-run checklist; dismissing it is remembered in this browser.
 */
export function WelcomeCard({ wsId }: { wsId: string }) {
  const [hidden, setHidden] = useState(welcomeDismissed());
  const [guideOpen, setGuideOpen] = useState(false);
  const tourApi = useTour();
  if (hidden) return null;
  const close = () => {
    dismissWelcome();
    setHidden(true);
  };
  return (
    <section className="card welcome-card" aria-labelledby="welcome-title">
      <div className="card-body">
        <h2 id="welcome-title" className="card-title">New to AnalystOS?</h2>
        <p className="welcome-text">
          It connects to your data, learns what it means, and then answers questions, runs investigations, prepares data and
          trains models — with every number checked and nothing published without an approval.
        </p>
        <div className="welcome-actions">
          <button type="button" className="btn btn-primary" onClick={() => { close(); tourApi.start("platform", wsId); }}>
            Take the 2-minute tour
          </button>
          <button type="button" className="btn" onClick={() => setGuideOpen(true)}>See everything it can do</button>
          <button type="button" className="btn btn-ghost" onClick={close}>Not now</button>
        </div>
      </div>
      {guideOpen && <GuideDrawer wsId={wsId} onClose={() => setGuideOpen(false)} />}
    </section>
  );
}
