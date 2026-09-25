"""Calibration of decisions (ADR-0015 §4, P4-T09).

Labelled outcomes come from real use and are attached to the decisions that made the call:

  finding accepted / rejected / dismissed  -> rev_second_opinion   (did the evidence support the claim?)
  alert acknowledged / investigated / dismissed -> alert_triage    (was the signal material?)
  feedback class corrected by the user      -> feedback_classification (the true kind)

A nightly job (scheduler, or `analystos calibrate`) computes per purpose x backend over a window:

  Brier  mean of 1/2 * sum_k (p_k - y_k)^2   (equals the usual (p - y)^2 for a yes/no decision)
  ECE    sum over 10 confidence bins of |bin|/n * |accuracy - mean confidence|

A model backend whose Brier or ECE is above the admin threshold (with at least the minimum number of
outcomes) is downgraded for that purpose: the DecisionService skips it and the next backend decides
(jev -> rules). Every change is a `decision_calibration` row and an audit event; a later evaluation
within threshold, or an administrator, restores it. `rules` is the last resort and is never downgraded.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput
from analystos.core.ids import utcnow
from analystos.core.logging import get_logger
from analystos.db.models import DecisionCalibration, DecisionOutcome, DecisionRecord, User
from analystos.decisions import store
from analystos.decisions.config import load
from analystos.decisions.types import BACKENDS, CALIBRATION_REQUIRED
from analystos.governance.audit import audit

log = get_logger(__name__)

# user signal -> {purpose: label on that purpose's scale}
SIGNALS: dict[str, dict[str, str]] = {
    "finding.accept": {"rev_second_opinion": "yes"},
    "finding.reject": {"rev_second_opinion": "no"},
    "finding.dismiss": {"rev_second_opinion": "no"},
    "alert.acknowledge": {"alert_triage": "yes"},
    "alert.investigate": {"alert_triage": "yes"},
    "alert.dismiss": {"alert_triage": "no"},
}
RUN_MARKER = "*"  # purpose/backend of the per-run heartbeat row
BINS = 10


# ------------------------------------------------------------------------------ outcomes
def record_signal(session: Session, source: str, subject: str, *, user_id: str | None,
                  workspace_id: str | None = None) -> int:
    """Label every decision about `subject` that the signal speaks to. Returns rows written."""
    labels = SIGNALS.get(source)
    if not labels:
        raise InvalidInput(f"unknown outcome signal {source}")
    return _label(session, subject, labels, source=source, user_id=user_id, workspace_id=workspace_id)


def record_correction(session: Session, purpose: str, subject: str, label: str, *, user_id: str | None,
                      workspace_id: str | None = None) -> int:
    """The user says the true answer of `purpose` about `subject` was `label` (e.g. a feedback kind)."""
    return _label(session, subject, {purpose: label}, source=f"{purpose}.correct", user_id=user_id, workspace_id=workspace_id)


def _label(session: Session, subject: str, labels: dict[str, str], *, source: str, user_id: str | None,
           workspace_id: str | None) -> int:
    rows = list(session.scalars(select(DecisionRecord).where(DecisionRecord.subject == subject,
                                                             DecisionRecord.purpose.in_(list(labels)))))
    for d in rows:
        session.add(DecisionOutcome(decision_id=d.id, purpose=d.purpose, backend=d.backend,
                                    workspace_id=workspace_id or d.workspace_id, label=labels[d.purpose][:80], source=source,
                                    user_id=user_id))
    return len(rows)


# ------------------------------------------------------------------------------ metrics
def distribution(probabilities: dict[str, Any] | None, answer: Any) -> dict[str, float]:
    """The decision's predictive distribution; a decision without probabilities cannot be calibrated."""
    probs = {str(k): float(v) for k, v in (probabilities or {}).items() if isinstance(v, (int, float))}
    total = sum(probs.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in probs.items()}


def brier(pairs: list[tuple[dict[str, float], str]]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for probs, label in pairs:
        classes = set(probs) | {label}
        total += 0.5 * sum((probs.get(k, 0.0) - (1.0 if k == label else 0.0)) ** 2 for k in classes)
    return round(total / len(pairs), 6)


def ece(pairs: list[tuple[dict[str, float], str]], bins: int = BINS) -> float | None:
    if not pairs:
        return None
    buckets: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for probs, label in pairs:
        predicted = max(probs, key=lambda k: probs[k])
        conf = probs[predicted]
        buckets[min(int(conf * bins), bins - 1)].append((conf, predicted == label))
    n = len(pairs)
    return round(sum(len(b) / n * abs(sum(c for _, c in b) / len(b) - sum(p for p, _ in b) / len(b)) for b in buckets.values()), 6)


# ------------------------------------------------------------------------------ evaluation
def _latest_rows(session: Session) -> dict[tuple[str, str], DecisionCalibration]:
    latest = select(DecisionCalibration.purpose, DecisionCalibration.backend, func.max(DecisionCalibration.id).label("id")) \
        .group_by(DecisionCalibration.purpose, DecisionCalibration.backend).subquery()
    rows = session.scalars(select(DecisionCalibration).join(latest, DecisionCalibration.id == latest.c.id))
    return {(r.purpose, r.backend): r for r in rows if r.purpose != RUN_MARKER}


def _restored_at(session: Session) -> dict[tuple[str, str], datetime]:
    """An administrator's restore resets the evidence: only decisions after it count."""
    rows = session.execute(select(DecisionCalibration.purpose, DecisionCalibration.backend,
                                  func.max(DecisionCalibration.created_at))
                           .where(DecisionCalibration.action == "admin_restored")
                           .group_by(DecisionCalibration.purpose, DecisionCalibration.backend)).all()
    return {(p, b): t for p, b, t in rows}


def measure(session: Session, *, window_days: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Brier and ECE per purpose x backend over labelled decisions in the window (latest label wins)."""
    now = now or utcnow()
    since = now - timedelta(days=window_days)
    latest_outcome = select(DecisionOutcome.decision_id, func.max(DecisionOutcome.id).label("id")) \
        .where(DecisionOutcome.created_at >= since).group_by(DecisionOutcome.decision_id).subquery()
    rows = session.execute(
        select(DecisionRecord.purpose, DecisionRecord.backend, DecisionRecord.probabilities, DecisionRecord.answer,
               DecisionRecord.created_at, DecisionOutcome.label)
        .join(latest_outcome, latest_outcome.c.decision_id == DecisionRecord.id)
        .join(DecisionOutcome, DecisionOutcome.id == latest_outcome.c.id)).all()
    restored = _restored_at(session)
    groups: dict[tuple[str, str], list[tuple[dict[str, float], str]]] = defaultdict(list)
    skipped: dict[tuple[str, str], int] = defaultdict(int)
    for purpose, backend, probabilities, answer, created_at, label in rows:
        key = (purpose, backend)
        cut = restored.get(key)
        if cut is not None and created_at is not None and created_at <= cut:
            continue
        probs = distribution(probabilities, answer)
        if not probs:
            skipped[key] += 1
            continue
        groups[key].append((probs, str(label)))
    out = []
    for key in sorted(set(groups) | set(skipped)):
        pairs = groups.get(key, [])
        out.append({"purpose": key[0], "backend": key[1], "n": len(pairs), "without_probabilities": skipped.get(key, 0),
                    "brier": brier(pairs), "ece": ece(pairs)})
    return out


def run_calibration(session: Session, *, actor: str = "scheduler", settings: Any = None, now: datetime | None = None,
                    dry_run: bool = False) -> dict[str, Any]:
    """Evaluate every labelled purpose x backend and downgrade/restore backends. Idempotent per state:
    a still-downgraded backend gets an `evaluated` row, not a second downgrade."""
    if settings is None:
        from analystos.services.platform_settings import get

        settings = get().decisions
    now = now or utcnow()
    purposes = load().purposes
    previous = _latest_rows(session)
    results = []
    for m in measure(session, window_days=settings.calibration_window_days, now=now):
        purpose, backend = m["purpose"], m["backend"]
        max_brier = settings.purpose_max_brier.get(purpose, settings.max_brier)
        was_down = bool(previous.get((purpose, backend)) and previous[(purpose, backend)].downgraded)
        enough = m["n"] >= settings.calibration_min_outcomes
        breach = enough and ((m["brier"] or 0.0) > max_brier or (m["ece"] or 0.0) > settings.max_ece)
        downgradable = backend not in ("rules", "default") and backend in BACKENDS
        pinned = f"{purpose}:{backend}" in settings.pinned
        if breach and downgradable and not pinned and settings.auto_downgrade:
            down, action = True, ("evaluated" if was_down else "downgraded")
            note = f"Brier {m['brier']} / ECE {m['ece']} above {max_brier} / {settings.max_ece} on {m['n']} outcomes"
        elif enough and not breach and was_down:
            down, action, note = False, "restored", f"back within threshold on {m['n']} outcomes"
        else:
            down, action = was_down, "evaluated"
            note = ("insufficient outcomes" if not enough else "rules is the last resort" if breach and not downgradable
                    else "pinned by an administrator" if breach and pinned else "auto_downgrade is off" if breach
                    else "within threshold")
        spec = purposes.get(purpose)
        row = {**m, "max_brier": max_brier, "max_ece": settings.max_ece, "downgraded": down, "action": action, "note": note,
               "authority": spec.authority if spec else None,
               "calibration_required": bool(spec and spec.authority in CALIBRATION_REQUIRED)}
        results.append(row)
        if dry_run:
            continue
        session.add(DecisionCalibration(purpose=purpose, backend=backend, window_days=settings.calibration_window_days, n=m["n"],
                                        brier=m["brier"], ece=m["ece"], max_brier=max_brier, max_ece=settings.max_ece,
                                        downgraded=down, action=action, note=note, created_by=actor))
        if action in ("downgraded", "restored"):
            audit(actor, f"decision.backend_{action}", target=f"{purpose}:{backend}", session=session,
                  details={k: row[k] for k in ("n", "brier", "ece", "max_brier", "max_ece", "note")})
    if not dry_run:
        session.add(DecisionCalibration(purpose=RUN_MARKER, backend=RUN_MARKER, window_days=settings.calibration_window_days,
                                        n=sum(r["n"] for r in results), downgraded=False, action="run",
                                        note=f"{len(results)} purpose x backend evaluated", created_by=actor))
        session.flush()
        store.invalidate()
    return {"at": now.isoformat(), "window_days": settings.calibration_window_days, "dry_run": dry_run, "results": results,
            "downgraded": sorted(f"{r['purpose']}:{r['backend']}" for r in results if r["downgraded"])}


def set_backend_state(session: Session, user: User, purpose: str, backend: str, *, downgraded: bool, note: str = "") -> dict:
    """Administrator override: restore a downgraded backend (its old evidence stops counting) or downgrade one."""
    if purpose not in load().purposes:
        raise InvalidInput(f"unknown decision purpose '{purpose}'")
    if backend not in BACKENDS or (backend == "rules" and downgraded):
        raise InvalidInput("backend must be jev, local_classifier or llm_structured (rules is the last resort)")
    action = "admin_downgraded" if downgraded else "admin_restored"
    session.add(DecisionCalibration(purpose=purpose, backend=backend, window_days=0, n=0, downgraded=downgraded, action=action,
                                    note=note or action, created_by=f"user:{user.id}"))
    audit(f"user:{user.id}", f"decision.backend_{action}", target=f"{purpose}:{backend}", details={"note": note}, session=session)
    session.flush()
    store.invalidate()
    return {"purpose": purpose, "backend": backend, "downgraded": downgraded, "action": action}


def report(session: Session) -> dict[str, Any]:
    """What Operate shows: thresholds, the latest evaluation per purpose x backend, effective downgrades,
    circuit breakers and each purpose's chain."""
    from analystos.decisions.breaker import snapshot
    from analystos.services.platform_settings import get

    settings = get().decisions
    latest = _latest_rows(session)
    last_run = session.scalar(select(func.max(DecisionCalibration.created_at)).where(DecisionCalibration.action == "run"))
    down = {k for k, r in latest.items() if r.downgraded}
    purposes = []
    for name, spec in sorted(load().purposes.items()):
        order = spec.ordered(settings.backends.get(name))
        purposes.append({"purpose": name, "authority": spec.authority, "calibration_required": spec.authority in CALIBRATION_REQUIRED,
                         "configured": order, "effective": [b for b in order if b == "rules" or (name, b) not in down],
                         "timeout_seconds": spec.timeout_seconds})
    return {
        "thresholds": {"max_brier": settings.max_brier, "max_ece": settings.max_ece, "purpose_max_brier": settings.purpose_max_brier,
                       "min_outcomes": settings.calibration_min_outcomes, "window_days": settings.calibration_window_days,
                       "auto_downgrade": settings.auto_downgrade, "pinned": settings.pinned},
        "last_run_at": last_run.isoformat() if last_run else None,
        "rows": [{"purpose": r.purpose, "backend": r.backend, "n": r.n, "brier": r.brier, "ece": r.ece, "max_brier": r.max_brier,
                  "max_ece": r.max_ece, "downgraded": r.downgraded, "action": r.action, "note": r.note, "by": r.created_by,
                  "at": r.created_at.isoformat() if r.created_at else None} for r in sorted(latest.values(), key=lambda r: (r.purpose, r.backend))],
        "downgraded": sorted(f"{p}:{b}" for p, b in down),
        "purposes": purposes,
        "breakers": snapshot(),
        "outcomes": dict(session.execute(select(DecisionOutcome.purpose, func.count()).group_by(DecisionOutcome.purpose)).all()),
    }


NIGHTLY_LOCK = 0x0DEC1510  # pg advisory lock: one scheduler process calibrates at a time
NIGHTLY_EVERY = timedelta(hours=24)


def maybe_run_nightly(session: Session, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Called from the scheduler loop: run once per 24 h across all scheduler processes."""
    now = now or utcnow()
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": NIGHTLY_LOCK}):
        return None
    last = session.scalar(select(func.max(DecisionCalibration.created_at)).where(DecisionCalibration.action == "run",
                                                                                   DecisionCalibration.created_by == "scheduler"))
    if last is not None and now - last < NIGHTLY_EVERY:
        return None
    result = run_calibration(session, actor="scheduler", now=now)
    log.info("decision calibration: %s evaluated, downgraded=%s", len(result["results"]), result["downgraded"])
    return result
