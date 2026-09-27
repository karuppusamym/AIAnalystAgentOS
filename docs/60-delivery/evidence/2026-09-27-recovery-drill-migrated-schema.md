# Recovery drill (P4-09) — 2026-09-27

Generated 2026-09-27 15:56 UTC by `scripts/recovery_drill.py` at `1d8d26c`. Source database `analystos_b2_migrated` on localhost:5432 (91 tables, 136 rows, Alembic revision `0043`); scratch database `analystos_b2_migrated_drill` (dropped after the drill).

**Result: PASSED** — backup `analystos_b2_migrated.dump` 275203 bytes, sha256 `f5ec51352f522df1…`

## Steps

| Step | Seconds |
|---|---|
| inventory source | 0.624 |
| resolve records on source | 1.138 |
| pg_dump (custom format) | 0.394 |
| create scratch database | 0.34 |
| pg_restore into scratch (copy 1) | 3.22 |
| destroy scratch database | 0.612 |
| recover: create + pg_restore (+ untar) | 4.211 |
| inventory restored copy | 0.111 |
| resolve records on restored copy | 0.033 |

## Checks on the recovered copy

| Check | Result | Detail |
|---|---|---|
| copy 1 equals the source | pass | 91 tables |
| scratch database destroyed | pass | analystos_b2_migrated_drill no longer exists |
| every table: same rows and checksum | pass | identical |
| alembic revision | pass | 0043 |
| runs resolve | pass | 0 runs {}; orphans: tasks 0, events 0 |
| approvals keep their hash binding | pass | 0/0 payloads hash to their approval {} |
| evidence resolves | pass | 0 insights; 0/0 bound evidence bundles hash to their record (source: 0/0) |
| verification records resolve | pass | 0/0 fingerprints recompute; 0 dependencies of 0 live records resolve to the same current version as on the source (0 still equal the recorded version) |
| schema matches this code | pass | every table and column the code maps exists |

## What resolved

* Runs: {'total': 0, 'statuses': {}, 'without_workspace': 0, 'orphan_tasks': 0, 'orphan_events': 0}
* Approvals: {'total': 0, 'statuses': {}, 'payload_hash_ok': 0, 'without_workspace': 0, 'run_missing': 0}
* Evidence: {'insights': 0, 'insight_hypothesis_missing': 0, 'insight_run_missing': 0, 'bundles_bound': 0, 'bundles_hash_ok': 0}
* Verification: {'records': 0, 'states': {}, 'fingerprint_ok': 0, 'live': 0, 'live_dependencies': 0, 'live_dependencies_current': 0, 'live_dependency_errors': 0, 'first_error': None}

## Scope and limits

* The drill proves a logical backup (`pg_dump`) of the control-plane database restores to an equivalent database and that AnalystOS resolves its records there. It is not a point-in-time recovery (WAL archiving) or a failover test; the recovery point is the time of the last backup.
* The analytics plane (staged snapshots, managed outputs) is a second database: back it up the same way (`--source-url` pointing at it); staged snapshots can also be re-staged from their sources, managed outputs cannot.
* The recovery time above is for this database's size on this host; it scales with the dump size.
* Runbook: `docs/30-runbooks/06-backup-and-recovery.md`.
