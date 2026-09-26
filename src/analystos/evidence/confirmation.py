"""Discovery versus confirmation (P4-03, target spec §4 "Validation").

Every finding produced by an adaptive round (model or rule hypotheses, drill-downs, novelty) is a
**discovery**: it is labelled ``exploratory`` even when its adjusted p-value passes, because the
hypotheses, groups and follow-ups were chosen on the same data. A finding becomes ``confirmed`` only
when one of these rules passes; nothing else — no model, no second method on the same data, no
confidence score — can set the label (`contracts.evidence.Validation` refuses it).

``holdout_partition``
    The claim (method, spec, top group, direction) was locked before an untouched partition was read,
    and the partition supports it with the same top group and direction. Input: a ``holdout`` record
    ``{partition, claim_locked_at, partition_accessed_at, supported, top, direction}`` produced by the
    confirming step. Fails when the lock is not strictly before access.

``fresh_snapshot_replication``
    A pre-registered claim (hypothesis origin ``registry`` or ``carried``: its spec was fixed by an
    earlier run and replayed without re-selection) is re-tested on a **different immutable data
    version** of the asset than the one it was discovered on, is verified again, and names the same top
    group and direction. The discovery version comes from the earlier finding's data-version manifest;
    a legacy finding without one cannot be the discovery half. The limit is recorded: a new snapshot
    usually overlaps the old rows, so this is a replication on new data, not a disjoint holdout.

A re-test on the *same* data version is a reproducibility check (state ``replicated``), never a
confirmation. Pushdown sources have no fixed version and cannot satisfy the snapshot rule.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from analystos.contracts.evidence import Confirmation, ManifestEntry

PREREGISTERED_ORIGINS = ("registry", "carried")


def _same(a: Any, b: Any) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def holdout_rule(holdout: Mapping[str, Any] | None, top: Any, direction: str | None) -> tuple[bool, str]:
    if not holdout:
        return False, "no held-out partition was evaluated"
    locked, accessed = str(holdout.get("claim_locked_at") or ""), str(holdout.get("partition_accessed_at") or "")
    if not locked or not accessed or not locked < accessed:
        return False, "the claim was not locked before the partition was read"
    if not holdout.get("supported"):
        return False, f"the held-out partition {holdout.get('partition')} does not support the claim"
    if not _same(holdout.get("top"), top) or (direction and holdout.get("direction") and holdout["direction"] != direction):
        return False, "the held-out partition names a different top group or direction"
    return True, f"confirmed on held-out partition {holdout.get('partition')}"


def snapshot_rule(origin: str | None, prior: Mapping[str, Any] | None, current: ManifestEntry | None, top: Any,
                  direction: str | None) -> tuple[bool, str]:
    if origin not in PREREGISTERED_ORIGINS:
        return False, f"hypothesis origin {origin or 'unknown'} was selected in this run, not pre-registered"
    if not prior:
        return False, "no earlier verified finding of this claim to replicate"
    if not prior.get("version"):
        return False, "the discovery run has no data-version manifest for this asset"
    if current is None or not current.immutable or not current.version:
        return False, "this run's data has no fixed version (pushdown or unrecorded snapshot)"
    if current.version == prior["version"]:
        return False, "same data version as the discovery run: a reproducibility check, not a confirmation"
    if not _same(prior.get("top"), top):
        return False, f"top group changed ({prior.get('top')} -> {top})"
    if direction and prior.get("direction") and prior["direction"] != direction:
        return False, f"direction changed ({prior.get('direction')} -> {direction})"
    return True, (f"pre-registered claim from run {prior.get('run_id')} replicated on a new snapshot "
                  f"({prior['version'][:12]} -> {current.version[:12]})")


def evaluate(*, verified: bool, origin: str | None, top: Any, direction: str | None,
             prior: Mapping[str, Any] | None = None, current: ManifestEntry | None = None,
             holdout: Mapping[str, Any] | None = None) -> Confirmation:
    """Try every rule; the first that passes confirms. A finding that failed REV is never confirmed."""
    tried: list[dict[str, Any]] = []
    for rule, (ok, why) in (("holdout_partition", holdout_rule(holdout, top, direction)),
                            ("fresh_snapshot_replication", snapshot_rule(origin, prior, current, top, direction))):
        ok = ok and verified
        tried.append({"rule": rule, "passed": ok, "reason": why if verified else "the finding did not pass REV"})
        if ok:
            return Confirmation(rule=rule, passed=True, evaluated=tried)
    return Confirmation(rule=None, passed=False, evaluated=tried)


def is_replication(origin: str | None, prior: Mapping[str, Any] | None, current: ManifestEntry | None, top: Any) -> bool:
    """A pre-registered claim re-tested on the same data version with the same answer."""
    return (origin in PREREGISTERED_ORIGINS and bool(prior) and current is not None
            and prior.get("version") == current.version and _same(prior.get("top"), top))


def prior_claim(session: Any, workspace_id: str, run_id: str, spec: Mapping[str, Any]) -> dict[str, Any] | None:
    """The most recent earlier verified finding of the same registered spec, with the data version of
    the asset it was computed on (None when there is none): the discovery half of the snapshot rule."""
    from sqlalchemy import select

    from analystos.db.models import Hypothesis, Insight
    from analystos.registries.hypotheses import spec_hash

    key, asset = spec_hash(dict(spec)), spec.get("asset")
    rows = session.execute(select(Insight, Hypothesis.spec).join(Hypothesis, Insight.hypothesis_id == Hypothesis.id)
                           .where(Insight.workspace_id == workspace_id, Insight.run_id != run_id,
                                  Insight.status == "verified")
                           .order_by(Insight.created_at.desc()).limit(500)).all()
    for ins, h_spec in rows:
        if not h_spec or h_spec.get("asset") != asset or spec_hash(dict(h_spec)) != key:
            continue
        bundle = dict(ins.evidence_bundle or {})
        entries = ((bundle.get("data") or {}).get("manifest") or {}).get("entries") or []
        entry = next((e for e in entries if e.get("asset") == asset), None)
        claim = bundle.get("claim") or {}
        return {"insight_id": ins.id, "run_id": ins.run_id, "version": (entry or {}).get("version"),
                "top": claim.get("subject"), "direction": claim.get("direction")}
    return None
