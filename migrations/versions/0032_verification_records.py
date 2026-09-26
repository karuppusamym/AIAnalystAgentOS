"""verification records with dependency fingerprints (P7-01, ADR-0020): `verification_record`,
`verification_dependency` (indexed on kind + ref), `verification_sweep`; existing findings mapped

Existing verified findings get a record (the mapping is copied here so the migration does not change when
the application code does; `tests/integration/test_verification_records_live.py` checks it against
`evidence.verification.fingerprint`):

* P4-03 **stale** (``stale_since`` set) -> ``VOID`` with ``void_kind = data`` and the recorded freshness reason;
* verified with a data-version manifest entry -> ``ACTIVE`` with the ``data`` dependency rebuilt from it (the
  only dependency whose recorded version can be reconstructed exactly); the next REV run records the rest;
* verified without one (legacy badges, P4-03 migration 0030) -> ``LEGACY``: shown without a verification badge.

Revision ID: 0032
Revises: 0030
Create Date: 2026-09-26 18:00:00

Hand-written; parallel streams: the coordinator re-chains `down_revision` on merge.
"""
import hashlib
import json
import secrets
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032"
down_revision = "0030"
branch_labels = None
depends_on = None

MIGRATION_VERIFIER = "migration.0032"


def _stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def fingerprint(deps):
    return _stable_hash(sorted(deps, key=lambda d: (d["kind"], d["ref"], d["version_hash"])))


def mapped_record(insight):
    """(state, dependencies, void) for one verified finding: the P7-01 mapping of P4-03 evidence."""
    bundle = insight["evidence_bundle"] or {}
    if isinstance(bundle, str):
        bundle = json.loads(bundle)
    entry = (bundle.get("data") or {}).get("entry") or {}
    deps = []
    if entry.get("asset") and bundle.get("version") != "evidence.legacy":
        deps.append({"kind": "data", "ref": f"{entry.get('source_id') or ''}/{entry['asset']}",
                     "version_hash": entry.get("version") or f"unversioned:{entry.get('mode') or 'unknown'}"})
    if insight["stale_since"] is not None:
        reason = (bundle.get("freshness") or {}).get("reason") or "data snapshot changed (P4-03 stale)"
        return "VOID", deps, {"kind": "data", "reason": reason, "at": insight["stale_since"]}
    if deps:
        return "ACTIVE", deps, None
    return "LEGACY", [], None


def upgrade() -> None:
    op.create_table(
        "verification_record",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("workspace_id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("subject_type", sa.String(length=30), nullable=False),
        sa.Column("subject_id", sa.String(length=80), nullable=False),
        sa.Column("question_hash", sa.String(length=64), nullable=True),
        sa.Column("verdict", sa.String(length=30), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verifier", sa.String(length=80), nullable=False),
        sa.Column("evidence_bundle_id", sa.String(length=100), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dependencies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("void_kind", sa.String(length=20), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column("void_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.String(length=40), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(op.f("ix_verification_record_workspace_id"), "verification_record", ["workspace_id"])
    op.create_index(op.f("ix_verification_record_run_id"), "verification_record", ["run_id"])
    op.create_index(op.f("ix_verification_record_question_hash"), "verification_record", ["question_hash"])
    op.create_index(op.f("ix_verification_record_state"), "verification_record", ["state"])
    op.create_index("ix_verification_record_subject", "verification_record", ["subject_type", "subject_id"])
    op.create_table(
        "verification_dependency",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("record_id", sa.String(length=40), sa.ForeignKey("verification_record.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("ref", sa.String(length=300), nullable=False),
        sa.Column("version_hash", sa.String(length=128), nullable=False),
    )
    op.create_index(op.f("ix_verification_dependency_record_id"), "verification_dependency", ["record_id"])
    op.create_index("ix_verification_dependency_kind_ref", "verification_dependency", ["kind", "ref"])
    op.create_table(
        "verification_sweep",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("actor", sa.String(length=80), nullable=False),
        sa.Column("checked", sa.Integer(), nullable=False),
        sa.Column("late_voids", sa.Integer(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, workspace_id, run_id, evidence_bundle, stale_since, verification FROM insight "
                                "WHERE status = 'verified' AND verified")).mappings().all()
    now = datetime.now(UTC)
    for r in rows:
        state, deps, void = mapped_record(r)
        rec_id = f"ver_{secrets.token_hex(6)}"
        checks = [c for c in ((r["verification"] or {}).get("evaluate") or []) if isinstance(c, dict)]
        conn.execute(sa.text(
            "INSERT INTO verification_record (id, workspace_id, run_id, subject_type, subject_id, verdict, checks, verifier, "
            "fingerprint, dependencies, state, void_kind, void_reason, void_detail, voided_at, flags, created_at) VALUES "
            "(:id, :ws, :run, 'insight', :sid, 'verified', CAST(:checks AS jsonb), :verifier, :fp, CAST(:deps AS jsonb), "
            ":state, :vk, :vr, CAST(:vd AS jsonb), :va, CAST('[]' AS jsonb), :at)"),
            {"id": rec_id, "ws": r["workspace_id"], "run": r["run_id"], "sid": r["id"], "checks": json.dumps(checks),
             "verifier": MIGRATION_VERIFIER, "fp": fingerprint(deps), "deps": json.dumps(deps), "state": state,
             "vk": void["kind"] if void else None, "vr": void["reason"] if void else None,
             "vd": json.dumps({"kind": "data", "event": "migration.0032", "late": False}) if void else None,
             "va": void["at"] if void else None, "at": now})
        for d in deps:
            conn.execute(sa.text("INSERT INTO verification_dependency (record_id, kind, ref, version_hash) "
                                 "VALUES (:rid, :kind, :ref, :v)"),
                         {"rid": rec_id, "kind": d["kind"], "ref": d["ref"], "v": d["version_hash"]})


def downgrade() -> None:
    op.drop_table("verification_sweep")
    op.drop_index("ix_verification_dependency_kind_ref", table_name="verification_dependency")
    op.drop_index(op.f("ix_verification_dependency_record_id"), table_name="verification_dependency")
    op.drop_table("verification_dependency")
    op.drop_index("ix_verification_record_subject", table_name="verification_record")
    op.drop_index(op.f("ix_verification_record_state"), table_name="verification_record")
    op.drop_index(op.f("ix_verification_record_question_hash"), table_name="verification_record")
    op.drop_index(op.f("ix_verification_record_run_id"), table_name="verification_record")
    op.drop_index(op.f("ix_verification_record_workspace_id"), table_name="verification_record")
    op.drop_table("verification_record")
