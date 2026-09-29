"""Held-out confirmation (P8-15): discovery on most rows, one locked test on the rest.

Each analysed table is split by a stable hash of its row key (or of the whole readable row when no key is
known) into a **discovery** part and a **held-out** part (`contracts.analysis.Partition`,
`analysis.holdout_fraction` of the rows, 30% by default). Hypothesis tests, the second method and the
follow-up rounds read only the discovery part, so whatever the run chose (hypotheses, groups, drill-downs)
was chosen without seeing the held-out rows.

After REV has verified a finding, its claim is **locked** (method, spec hash, top group, baseline,
direction, time) and the same test runs **once** on the held-out part. The partition predicate refuses to
compile the held-out side without a lock time, and the access time is taken strictly after it, so
`confirmation.holdout_rule` can check the order. The held-out test is the finding's own comparison:

* a segment claim over more than two plain categorical groups is re-tested as top group vs baseline group
  (the comparison the finding states), with the same method;
* a two-group comparison has a direction, and the locked claim fixed it in advance, so it is judged
  one-sided: the same test at 2 x alpha, counted only when the same group comes out on top;
* anything else (trends, correlations, concentration, driver models, derived segments) is re-run as-is at
  alpha, two-sided, and must name the same top group / direction.

A held-out part smaller than the minimum sample does not confirm. What this cannot do: a pattern that is a
fluke of the whole table (both parts share it) can still pass; the strength label (`evidence.strength`)
is the other half of the guard. Same data -> same partition -> same result.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from analystos.contracts.analysis import AnalysisSpec, Filter, Partition
from analystos.contracts.evidence import HoldoutCheck
from analystos.core.errors import InvalidInput
from analystos.core.ids import stable_hash
from analystos.methods.base import OTHER
from analystos.skills.sqlbuild import PARTITION_DIALECTS, _check_dialect

KEY_EVIDENCE = ("approved", "declared", "measured_unique", "profile_unique")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def strictly_after(ts: str) -> str:
    """The current time, strictly later than `ts` (ISO strings compare in time order at a fixed format)."""
    t = now_iso()
    while t <= ts:
        t = now_iso()
    return t


# ------------------------------------------------------------------------------------ the partition
def readable_columns(scope: Any, asset: str) -> list[str]:
    denied = {d.lower() for d in getattr(scope, "denied_columns", []) or []}
    table = asset.split(".")[-1].lower()
    out = []
    for c in (getattr(scope, "columns", {}) or {}).get(asset) or []:
        cl = c.lower()
        if f"{asset.lower()}.{cl}" in denied or f"*.{cl}" in denied or f"{table}.{cl}" in denied:
            continue
        out.append(c)
    return out


def catalog_facts(session: Any, source_id: str | None, asset: str) -> tuple[tuple[list[str], str] | None, int | None]:
    """(the table's key from the catalog: approved, declared or measured unique, else None; its row count)."""
    from sqlalchemy import select

    from analystos.db.models import SourceAsset, SourceColumn
    from analystos.semantic.suggest import primary_key

    parts = asset.split(".")
    if not source_id or len(parts) < 2:
        return None, None
    row = session.scalar(select(SourceAsset).where(SourceAsset.source_id == source_id, SourceAsset.schema_name == parts[-2],
                                                   SourceAsset.name == parts[-1]))
    if row is None:
        return None, None
    cols = list(session.scalars(select(SourceColumn).where(SourceColumn.asset_id == row.id).order_by(SourceColumn.ordinal)))
    key = primary_key(row, cols, None)
    if key.get("evidence") not in KEY_EVIDENCE or not key.get("columns") or key.get("unique") is False:
        return None, row.row_count
    return (list(key["columns"]), f"{key['evidence']}_key"), row.row_count


def discovery_partition(*, fraction: float, dialect: str, readable: list[str], key: tuple[list[str], str] | None = None,
                        rows: int | None = None, min_rows: int = 0) -> tuple[Partition | None, str | None]:
    """(the discovery partition, None) or (None, why there is no held-out confirmation for this table)."""
    if fraction <= 0:
        return None, "held-out confirmation is off (analysis.holdout_fraction is 0)"
    try:
        known = _check_dialect(dialect) in PARTITION_DIALECTS
    except InvalidInput:
        known = False
    if not known:
        return None, f"the {dialect} compiler has no stable row hash to hold rows out by"
    if rows is not None and min_rows and rows * fraction < min_rows:
        return None, f"the table has {rows} rows: too few to hold {fraction:.0%} back and keep {min_rows} for the check"
    cols, basis = key if key and set(key[0]) <= set(readable) else (readable, "full_row")
    if not cols:
        return None, "no readable column to split the rows by"
    return Partition(side="discovery", key=list(cols), basis=basis, fraction=fraction), None


def describe(p: Partition) -> str:
    by = f"by {', '.join(p.key)}" if p.basis != "full_row" else "by the whole row (no key known)"
    return f"{p.fraction:.0%} of rows held out {by}"


def partition_record(p: Partition | None, reason: str | None) -> dict[str, Any]:
    """What a primary result records about the rows it read (`details.partition`)."""
    if p is None:
        return {"applied": False, "reason": reason}
    return {"applied": True, "side": p.side, "description": describe(p), **p.model_dump(exclude={"side", "claim_locked_at"})}


def from_record(rec: Mapping[str, Any] | None) -> Partition | None:
    if not rec or not rec.get("applied"):
        return None
    return Partition(side="discovery", key=list(rec["key"]), basis=rec.get("basis") or "full_row",
                     fraction=float(rec["fraction"]), salt=rec.get("salt") or Partition.model_fields["salt"].default)


# ------------------------------------------------------------------------------------ lock, then test once
class LockedClaim:
    """A claim fixed before the held-out rows are read. Only `lock_claim` makes one."""

    __slots__ = ("claim", "claim_hash", "locked_at")

    def __init__(self, claim: dict[str, Any], locked_at: str):
        self.claim, self.locked_at = claim, locked_at
        self.claim_hash = stable_hash({**claim, "locked_at": locked_at})


def lock_claim(*, spec: Mapping[str, Any], stat: Mapping[str, Any], top: Any, direction: str | None,
               spec_hash: str) -> LockedClaim:
    hl = stat.get("highlights") or {}
    claim = {"method": spec.get("method"), "asset": spec.get("asset"), "spec_hash": spec_hash,
             "top": None if top is None else str(top), "baseline": hl.get("baseline_segment"), "direction": direction,
             "test": stat.get("test"), "groups": len(stat.get("groups") or [])}
    return LockedClaim(claim, now_iso())


def _numeric(v: Any) -> bool:
    try:
        float(str(v))
        return True
    except ValueError:
        return False


def claim_spec(spec: AnalysisSpec, claim: Mapping[str, Any]) -> tuple[AnalysisSpec, list[str] | None]:
    """The spec the held-out test runs: the finding's own top-vs-baseline comparison when the discovery
    compared more than two plain categorical groups, else the spec unchanged."""
    top, base = claim.get("top"), claim.get("baseline")
    seg = spec.segment
    if (seg is None or seg.type != "column" or (claim.get("groups") or 0) <= 2 or top is None or base is None
            or top == base or OTHER in (top, base) or "None" in (top, base) or _numeric(top) or _numeric(base)):
        return spec, None
    focus = Filter(column=seg.column, op="in", value=[top, base])
    return spec.model_copy(update={"filters": [*spec.filters, focus]}), [top, base]


def direction_of(spec: Mapping[str, Any], stat: Mapping[str, Any]) -> str | None:
    """The direction of a result's headline comparison, from the method's typed facts."""
    from analystos import methods
    from analystos.evidence.facts import facts_from

    name = spec.get("method")
    facts = (methods.get(name).facts if name in methods.names() else facts_from)(spec, stat)
    primary = next((f for f in facts if getattr(f, "primary", False)), None)
    return getattr(primary, "direction", None)


def top_of(stat: Mapping[str, Any]) -> str | None:
    hl = stat.get("highlights") or {}
    top = hl.get("top_segment", hl.get("top_driver"))
    return None if top is None else str(top)


def test_on_holdout(spec: AnalysisSpec, run_sql: Any, locked: LockedClaim, discovery: Partition, *, alpha: float,
                    min_n: int, sample_rows: int = 50000,
                    runner: Callable[..., Any] | None = None) -> tuple[HoldoutCheck, Any]:
    """Run the locked claim's test once on the held-out rows. Returns (the record, the analysis outcome)."""
    from analystos.skills.analysis import run_analysis

    run = runner or run_analysis
    held = discovery.model_copy(update={"side": "holdout", "claim_locked_at": locked.locked_at})
    test_spec, contrast = claim_spec(spec, locked.claim)
    two_groups = spec.segment is not None and (contrast is not None or locked.claim.get("groups") == 2)
    accessed = strictly_after(locked.locked_at)
    outcome = run(test_spec, run_sql, alpha=2 * alpha if two_groups else alpha, sample_rows=sample_rows, partition=held)
    stat = outcome.stat.model_dump() if hasattr(outcome.stat, "model_dump") else dict(outcome.stat)
    top, direction = top_of(stat), direction_of(test_spec.model_dump(), stat)
    p = stat.get("p_value")
    one_sided = two_groups and len(stat.get("groups") or []) == 2
    significant = bool(stat.get("supported")) and (one_sided or (p is not None and p < alpha))
    claim_top, claim_direction = locked.claim.get("top"), locked.claim.get("direction")
    n = int(stat.get("n") or 0)
    reason = None
    if n < min_n:
        reason = f"the held-out part has {n} rows, under the minimum of {min_n}"
    elif not significant:
        reason = "the held-out rows do not show the effect at the required size and significance"
    elif claim_top is not None and top != claim_top:
        reason = f"the held-out rows put {top} on top, not {claim_top}"
    elif claim_direction and direction and direction != claim_direction:
        reason = f"the held-out rows point the other way ({direction}, not {claim_direction})"
    supported = reason is None
    record = HoldoutCheck(
        evaluated=True, reason=reason, partition=describe(discovery), partition_spec=held.model_dump(exclude={"claim_locked_at"}),
        claim=dict(locked.claim), claim_hash=locked.claim_hash, claim_locked_at=locked.locked_at,
        partition_accessed_at=accessed, supported=supported, top=top, direction=direction, test=stat.get("test"),
        p_value=p, p_one_sided=(p / 2 if top == locked.claim.get("top") else 1 - p / 2) if one_sided and p is not None else None,
        alpha=alpha, effect_size=stat.get("effect_size"), effect_label=stat.get("effect_label"), n=n, contrast=contrast)
    return record, outcome


def not_evaluated(reason: str) -> HoldoutCheck:
    return HoldoutCheck(evaluated=False, reason=reason, supported=False)
