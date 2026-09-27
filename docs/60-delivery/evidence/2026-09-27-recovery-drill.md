# Recovery drill (P4-09) — 2026-09-27

Generated 2026-09-27 15:56 UTC by `scripts/recovery_drill.py` at `unknown`. Source database `analystos_test_b2` on localhost:5432 (90 tables, 20245 rows, Alembic revision none recorded (the schema was not created by migrations)); scratch database `analystos_test_b2_drill` (dropped after the drill).

**Result: PASSED** — backup `analystos_test_b2.dump` 2912830 bytes, sha256 `44f876c23f3042bd…`; files archive 43 files, 294523 bytes

## Steps

| Step | Seconds |
|---|---|
| inventory source | 0.941 |
| resolve records on source | 2.954 |
| pg_dump (custom format) | 0.929 |
| tar artifact files | 0.161 |
| create scratch database | 0.159 |
| pg_restore into scratch (copy 1) | 5.593 |
| destroy scratch database | 0.81 |
| recover: create + pg_restore (+ untar) | 6.011 |
| inventory restored copy | 0.467 |
| resolve records on restored copy | 0.992 |

## Checks on the recovered copy

| Check | Result | Detail |
|---|---|---|
| copy 1 equals the source | pass | 90 tables |
| scratch database destroyed | pass | analystos_test_b2_drill no longer exists |
| every table: same rows and checksum | pass | identical |
| alembic revision | pass | none on the source either (the schema was not created by migrations) |
| runs resolve | pass | 31 runs {'COMPLETED': 31}; orphans: tasks 0, events 0 |
| approvals keep their hash binding | pass | 38/38 payloads hash to their approval {'approved': 1, 'executed': 1, 'expired': 1, 'invalidated': 1, 'pending': 34} |
| evidence resolves | pass | 45 insights; 45/45 bound evidence bundles hash to their record (source: 45/45) |
| verification records resolve | pass | 55/55 fingerprints recompute; 269 dependencies of 55 live records resolve to the same current version as on the source (269 still equal the recorded version) |
| schema matches this code | pass | every table and column the code maps exists |
| files restore byte-for-byte | pass | 43 files |

## What resolved

* Runs: {'total': 31, 'statuses': {'COMPLETED': 31}, 'without_workspace': 0, 'orphan_tasks': 0, 'orphan_events': 0}
* Approvals: {'total': 38, 'statuses': {'approved': 1, 'executed': 1, 'expired': 1, 'invalidated': 1, 'pending': 34}, 'payload_hash_ok': 38, 'without_workspace': 0, 'run_missing': 0}
* Evidence: {'insights': 45, 'insight_hypothesis_missing': 0, 'insight_run_missing': 0, 'bundles_bound': 45, 'bundles_hash_ok': 45}
* Verification: {'records': 55, 'states': {'ACTIVE': 55}, 'fingerprint_ok': 55, 'live': 55, 'live_dependencies': 269, 'live_dependencies_current': 269, 'live_dependency_errors': 0, 'first_error': None}

## Scope and limits

* The drill proves a logical backup (`pg_dump`) of the control-plane database restores to an equivalent database and that AnalystOS resolves its records there. It is not a point-in-time recovery (WAL archiving) or a failover test; the recovery point is the time of the last backup.
* The analytics plane (staged snapshots, managed outputs) is a second database: back it up the same way (`--source-url` pointing at it); staged snapshots can also be re-staged from their sources, managed outputs cannot.
* The recovery time above is for this database's size on this host; it scales with the dump size.
* Runbook: `docs/30-runbooks/06-backup-and-recovery.md`.

## Reading the result (added by hand, 2026-09-27)

* **Code.** "at `unknown`": this drill ran from an extract of commit `0a16968` (`git archive 0a16968 src config`,
  with the drill script as committed plus its schema check) — the code that wrote the source database. Run from the
  working tree, whose models already carried the P4-09 `owners` columns, the same drill **failed**, as it should:
  `schema matches this code` listed `source.owners, workspace.owners`, and 145 of 269 verification dependencies
  could not be resolved (`UndefinedColumn`). The first version of the drill script hid that failure by comparing
  error strings; it now fails on any unresolvable dependency and checks the schema against the code explicitly.
  Runbook step 3 (migrate a restored copy before starting the platform) is the remedy it names.
* **Source.** `analystos_test_b2` is the control-plane database the held-out v2 platform run
  (`2026-09-27-heldout-v2-component-platform-off.md`) had just written: 31 completed analysis runs, 45 verified
  insights with evidence bundles, 55 verification records, 38 approvals (the governance scenarios' expired,
  invalidated, executed and approved ones among them), recipe runs, ML experiments and definitions. Its schema was
  created by the test fixture (`create_all`), so it carries no Alembic revision. `--files` is the artifact directory
  the same run wrote (recipe snapshots, ML artifacts); `ANALYSTOS_ARTIFACT_DIR` pointed at it so that ML dependencies
  resolve against the restored files (all 269 dependencies of the 55 live records equal their recorded versions).
* **Migrated schema.** A second drill on a freshly migrated (`alembic upgrade head` = `0043`, after a `downgrade -1` /
  `upgrade head` round trip) and seeded database passed the same checks with the working-tree code and kept the
  revision: `2026-09-27-recovery-drill-migrated-schema.md`.
* **Recovery time.** Create + restore of a 2.9 MB dump (20,245 rows) and the artifact tar took about 6-7 s on a shared
  4-CPU host under load; it scales with the dump size. The recovery point is the time of the dump (no WAL archiving).
* **Not covered.** The analytics database (staged snapshots, managed outputs) was not drilled here; the same script
  drills it with `--source-url` pointing at it. Failover and point-in-time recovery are out of scope.
