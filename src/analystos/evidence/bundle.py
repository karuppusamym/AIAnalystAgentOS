"""Evidence bundle assembly, missing-evidence refusal and legacy badge mapping (P4-03).

`assemble` builds the versioned `EvidenceBundle` for one finding at REV time from deterministic inputs
only (the statistic, the second method, the REV checks, the governed query receipts, the run's data
manifest and the bound facts). `missing_evidence` lists the dimensions a finding needs but lacks; a
finding with any is refused: it fails the ``evidence_complete`` check and its state is
``insufficient_evidence`` ("insufficient evidence" is a valid outcome, not an error).

Required for every finding: governed query receipts with result hashes, a data-version manifest entry
for the analysed asset, typed facts that bind the narrative, the effect size, sample sizes, the
population check and the method-fit (assumption) check. Required when the method has them: the
multiple-testing family (any p-value), an uncertainty interval (unless the method declares it
optional via `AnalysisMethod.evidence_optional`).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from analystos.contracts.evidence import (
    Binding,
    Check,
    Confirmation,
    EvidenceBundle,
    Fact,
    Freshness,
    ManifestEntry,
    ReviewScore,
    Validation,
)

# REV checks whose failure means the claim as stated is wrong, rather than not established.
INVALIDATING = ("fact_binding", "method_fit")
EVIDENCE_CHECK = "evidence_complete"


def power_at_threshold(stat: Mapping[str, Any], *, alpha: float = 0.05) -> dict[str, Any]:
    """Approximate power to detect the method's practical threshold with the group sizes observed (sample
    adequacy, target spec §4). Stated where the result shape allows a closed form: two proportions (top vs
    baseline rate, threshold = the minimum rate ratio). Elsewhere recorded as not assessed, never guessed."""
    from scipy.stats import norm

    from analystos.skills.stats import MIN_RATE_RATIO

    hl = stat.get("highlights") or {}
    p0, n1, n0 = hl.get("baseline_rate"), hl.get("top_n"), hl.get("baseline_n")
    if not all(isinstance(v, int | float) for v in (p0, n1, n0)) or not n1 or not n0 or not 0 < float(p0) < 1:
        return {"assessed": False, "reason": "no closed-form power rule for this result shape"}
    p0, n1, n0 = float(p0), float(n1), float(n0)
    p1 = min(p0 * MIN_RATE_RATIO, 0.999)
    pbar = (p1 * n1 + p0 * n0) / (n1 + n0)
    se0 = math.sqrt(pbar * (1 - pbar) * (1 / n1 + 1 / n0))
    se1 = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
    power = float(norm.cdf((abs(p1 - p0) - norm.ppf(1 - alpha / 2) * se0) / se1))
    return {"assessed": True, "rule": "two_proportion_z", "threshold": {"rate_ratio": MIN_RATE_RATIO},
            "baseline_rate": p0, "n_top": int(n1), "n_baseline": int(n0), "alpha": alpha, "power": round(power, 4),
            "adequate": power >= 0.8}


def _uncertainty(stat: Mapping[str, Any], second: Mapping[str, Any] | None) -> dict[str, Any] | None:
    for source, s in (("primary", stat), ("second_method", second or {})):
        lo, hi = s.get("ci_low"), s.get("ci_high")
        if isinstance(lo, int | float) and isinstance(hi, int | float):
            return {"source": source, "low": lo, "high": hi, "of": s.get("effect_label"), "test": s.get("test"),
                    "level": 0.95}
    groups = [g for g in stat.get("groups") or [] if g.get("ci_low") is not None and g.get("ci_high") is not None]
    if groups:
        return {"source": "primary_groups", "of": "per-group estimate", "level": 0.95,
                "groups": {str(g.get("segment")): [g["ci_low"], g["ci_high"]] for g in groups[:50]}}
    return None


def _thresholds(stat: Mapping[str, Any]) -> dict[str, Any]:
    d = stat.get("details") or {}
    out = {k: v for k, v in d.items() if k.startswith("threshold") and not isinstance(v, dict)}
    out.update(d.get("thresholds") or {})
    return out


def method_dimensions(spec: Mapping[str, Any], stat: Mapping[str, Any], second: Mapping[str, Any] | None, *,
                      family_size: int, alpha: float, origin: str | None, iteration: int | None,
                      parent: str | None) -> dict[str, Any]:
    hl = stat.get("highlights") or {}
    details = stat.get("details") or {}
    groups = [g for g in stat.get("groups") or [] if isinstance(g.get("n"), int | float)]
    p = stat.get("p_value")
    return {
        "test": stat.get("test"), "method": spec.get("method"),
        "effect": {"value": stat.get("effect_size"), "label": stat.get("effect_label"),
                   "practical_threshold": _thresholds(stat) or None},
        "uncertainty": _uncertainty(stat, second),
        "sample_sizes": {"n": stat.get("n"), "by_group": {str(g.get("segment")): g["n"] for g in groups[:100]} or None,
                         "smallest_group": min((g["n"] for g in groups), default=None)},
        "multiple_testing": {"procedure": "benjamini_hochberg", "family": "primary tests of this run",
                             "family_size": family_size, "p": p, "q": stat.get("p_adjusted"), "alpha": alpha}
        if p is not None else None,
        "selection": {"origin": origin, "round": iteration, "parent": parent,
                      "top_group_selected_post_hoc": hl.get("top_segment") is not None or hl.get("top_driver") is not None,
                      "groups_compared": hl.get("n_groups") or (len(groups) or None),
                      "excluded": {k: details[k] for k in ("small_segments", "small_segment_rows", "beyond_top_k_segments",
                                                           "beyond_top_k_rows") if details.get(k)} or None},
        "assumptions": {"stated": list(stat.get("assumptions") or []), "warnings": list(stat.get("warnings") or [])[:10]},
        "second_method": {k: (second or {}).get(k) for k in ("test", "p_value", "effect_size", "effect_label")}
        if second else None,
        "power": power_at_threshold(stat, alpha=alpha),
    }


def missing_evidence(bundle: Mapping[str, Any], *, optional: Sequence[str] = ()) -> list[str]:
    """Dimensions this finding needs but lacks (empty = complete). Fail closed: absent is missing."""
    data, claim, method = bundle.get("data") or {}, bundle.get("claim") or {}, bundle.get("method") or {}
    checks = {c.get("check") for c in (bundle.get("validation") or {}).get("checks") or []}
    out = []
    receipts = data.get("queries") or []
    if not receipts or not all(q.get("result_hash") for q in receipts):
        out.append("data.queries")
    if not data.get("entry"):
        out.append("data.manifest")
    if "representative_population" not in checks:
        out.append("data.population")
    if not claim.get("facts"):
        out.append("claim.facts")
    if not (claim.get("binding") or {}).get("mentions") and not (claim.get("binding") or {}).get("ok"):
        out.append("claim.binding")
    if (method.get("effect") or {}).get("value") is None:
        out.append("method.effect_size")
    if not (method.get("sample_sizes") or {}).get("n"):
        out.append("method.sample_sizes")
    if "method_fit" not in checks:
        out.append("method.assumptions")
    if method.get("multiple_testing") is None and method.get("has_p_value"):
        out.append("method.multiple_testing")
    if method.get("uncertainty") is None and "uncertainty" not in optional:
        out.append("method.uncertainty")
    return out


def state_for(checks: Sequence[Check], missing: Sequence[str], confirmation: Confirmation,
              replicated: bool) -> tuple[str, str]:
    """(state, label) from check outcomes: refusal first, then a claim contradicted by its own evidence,
    then a claim not established, then the confirmation rules."""
    if missing:
        return "insufficient_evidence", "discovery"
    failed = [c.check for c in checks if c.outcome == "fail"]
    if any(c in INVALIDATING for c in failed):
        return "invalid", "discovery"
    if failed:
        return "inconclusive", "discovery"
    if confirmation.passed:
        return "confirmed", "confirmation"
    return ("replicated" if replicated else "exploratory"), "discovery"


def as_checks(rev_checks: Sequence[Mapping[str, Any]]) -> list[Check]:
    out = []
    for c in rev_checks:
        outcome = c.get("outcome") or ("pass" if c.get("passed") else "fail")
        extra = {k: v for k, v in c.items() if k not in ("check", "passed", "outcome", "detail", "reason")}
        out.append(Check(check=str(c.get("check")), outcome=outcome, reason=str(c.get("reason") or c.get("detail") or ""),
                         **extra))
    return out


def assemble(*, spec: Mapping[str, Any], stat: Mapping[str, Any], second: Mapping[str, Any] | None,
             facts: Sequence[Fact], binding: Binding, rev_checks: Sequence[Mapping[str, Any]],
             receipts: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any] | None, entry: ManifestEntry | None,
             population: Mapping[str, Any] | None, caveats: Sequence[str], confirmation: Confirmation,
             replicated: bool, reproducible: bool | None, review_score: float | None, family_size: int, alpha: float,
             origin: str | None, iteration: int | None, parent: str | None, claim_meta: Mapping[str, Any],
             optional: Sequence[str] = (), predictive: bool = False) -> EvidenceBundle:
    """The finding's bundle; its `validation.missing_evidence` lists what refused it (if anything)."""
    method = method_dimensions(spec, stat, second, family_size=family_size, alpha=alpha, origin=origin,
                               iteration=iteration, parent=parent)
    method["has_p_value"] = stat.get("p_value") is not None
    data = {"asset": spec.get("asset"), "queries": list(receipts), "filters": list(spec.get("filters") or []),
            "manifest": {"version": (manifest or {}).get("version"), "entries": [entry.model_dump(mode="json")] if entry else []},
            "entry": entry.model_dump(mode="json") if entry else None, "population": dict(population or {}) or None,
            "excluded_rows": {k: v for k, v in (stat.get("details") or {}).items() if k.startswith("excluded_")} or None}
    claim = {**claim_meta, "facts": [f.model_dump(mode="json") for f in facts], "binding": binding.model_dump(mode="json")}
    checks = as_checks(rev_checks)
    draft = {"data": data, "claim": claim, "method": method,
             "validation": {"checks": [c.model_dump() for c in checks]}}
    missing = missing_evidence(draft, optional=optional)
    checks.append(Check(check=EVIDENCE_CHECK, outcome="fail" if missing else "pass",
                        reason=("missing: " + ", ".join(missing)) if missing else "all required dimensions present"))
    state, label = state_for(checks, missing, confirmation, replicated)
    limits = {"population": [c for c in caveats if c], "confounding": "unidentified: associations in historical data",
              "untested": (method.get("selection") or {}).get("excluded"),
              "replay": "exact (immutable snapshot)" if entry is not None and entry.immutable else
              "best-effort (no fixed data version)",
              "confirmation_overlap": "a new snapshot may share rows with the discovery snapshot"
              if confirmation.rule == "fresh_snapshot_replication" else None}
    return EvidenceBundle(data=data, claim=claim, method=method, limits=limits,
                          validation=Validation(state=state, label=label, checks=checks, confirmation=confirmation,
                                                missing_evidence=missing, reproducible=reproducible,
                                                predictive_evaluated=predictive),
                          freshness=Freshness(state="current"), review_score=ReviewScore(value=review_score))


# ------------------------------------------------------------------------------------ legacy badges
LEGACY_VERIFIER = "rev.v1"


def legacy_bundle(status: str | None, verified: bool | None, confidence: float | None,
                  verification: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
    """Map a pre-P4-03 finding's badge onto the evidence model: (bundle, validation state).

    ADR-0011: keep the historical `verified` / confidence values with their verifier version and never
    silently upgrade them. A legacy `verified` finding passed the v1 REV checks (reproducible re-run,
    same-data second method, BH significance); that is at most exploratory evidence, so it maps to state
    ``legacy`` with ``equivalent: exploratory`` and is never ``confirmed``. Failed findings map to
    ``inconclusive``, rejected ones to ``invalid``; anything else stays ``legacy`` (unverified)."""
    v = dict(verification or {})
    rev = [c for c in v.get("evaluate") or [] if isinstance(c, dict) and c.get("check")]
    checks = [{"check": str(c["check"]), "outcome": "pass" if c.get("passed") else "fail",
               "reason": str(c.get("detail") or "")} for c in rev]
    if status == "verified" and verified:
        state, equivalent = "legacy", "exploratory"
    elif status == "failed_verification":
        state, equivalent = "inconclusive", "inconclusive"
    elif status == "rejected":
        state, equivalent = "invalid", "invalid"
    else:
        state, equivalent = "legacy", "unverified"
    label = "legacy" if state == "legacy" else "discovery"
    bundle = {
        "version": "evidence.legacy", "verifier_version": LEGACY_VERIFIER,
        "data": {}, "claim": {}, "method": {}, "limits": {"note": "recorded before typed evidence (P4-03); "
                                                                  "no data-version manifest or fact binding"},
        "validation": {"state": state, "label": label, "checks": checks,
                       "confirmation": {"rule": None, "passed": False, "evaluated": []},
                       "missing_evidence": ["data.manifest", "claim.facts", "claim.binding"],
                       "reproducible": (v.get("verify") or {}).get("reproducible"), "predictive_evaluated": False},
        "freshness": {"state": "unknown", "since": None, "reason": "no data-version manifest recorded", "assets": []},
        "review_score": {"value": confidence, "calibrated": False,
                         "note": "heuristic review score from deterministic checks; not a calibrated probability"},
        "legacy": {"status": status, "verified": bool(verified), "confidence": confidence,
                   "verifier_version": LEGACY_VERIFIER, "equivalent": equivalent},
    }
    return bundle, state
