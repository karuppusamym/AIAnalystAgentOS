"""typed evidence on findings: evidence bundle, validation state, data version, staleness; run data
manifest; legacy badge migration (P4-03)

Existing findings keep their `verified` / `confidence` values; their badge is mapped onto the evidence
model by `legacy_bundle` below (same mapping as `analystos.evidence.bundle.legacy_bundle`, copied so
the migration does not change when the application code does): a legacy verified finding becomes state
`legacy` (equivalent: exploratory, never confirmed), failed -> `inconclusive`, rejected -> `invalid`.

Revision ID: 0030
Revises: 0024
Create Date: 2026-09-26 12:00:00

Hand-written; parallel increment-4 streams: the coordinator re-chains `down_revision` on merge.
"""
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0024"
branch_labels = None
depends_on = None

LEGACY_VERIFIER = "rev.v1"


def legacy_bundle(status, verified, confidence, verification):
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


def upgrade() -> None:
    op.add_column("insight", sa.Column("evidence_bundle", postgresql.JSONB(astext_type=sa.Text()), server_default="{}",
                                       nullable=False))
    op.add_column("insight", sa.Column("validation", sa.String(length=30), server_default="legacy", nullable=False))
    op.add_column("insight", sa.Column("data_version", sa.String(length=64), nullable=True))
    op.add_column("insight", sa.Column("stale_since", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_insight_data_version"), "insight", ["data_version"], unique=False)
    op.add_column("analysis_run", sa.Column("data_manifest", postgresql.JSONB(astext_type=sa.Text()), server_default="{}",
                                            nullable=False))
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, status, verified, confidence, verification FROM insight")).fetchall()
    for r in rows:
        bundle, state = legacy_bundle(r.status, r.verified, r.confidence, r.verification)
        conn.execute(sa.text("UPDATE insight SET evidence_bundle = CAST(:b AS jsonb), validation = :s WHERE id = :id"),
                     {"b": json.dumps(bundle), "s": state, "id": r.id})


def downgrade() -> None:
    op.drop_column("analysis_run", "data_manifest")
    op.drop_index(op.f("ix_insight_data_version"), table_name="insight")
    op.drop_column("insight", "stale_since")
    op.drop_column("insight", "data_version")
    op.drop_column("insight", "validation")
    op.drop_column("insight", "evidence_bundle")
