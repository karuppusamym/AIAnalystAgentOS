# Runbook — backup, restore and the recovery drill (P4-09)

What must survive a lost database, how to back it up, how to restore it, and the drill that proves a restore
still resolves runs, approvals, evidence and verification records. The latest drill evidence is
`docs/60-delivery/evidence/<date>-recovery-drill.md`.

## What holds state

| Store | Holds | Loss means | Back up with |
|---|---|---|---|
| Control-plane Postgres (`ANALYSTOS_DATABASE_URL`) | workspaces, members, policies, sources and catalog, runs, tasks, events, hypotheses, insights and their evidence bundles, approvals (payload and plan hashes), verification records, definitions, schedules, recipes and runs, ML registry, audit | everything a user sees and every governance record | `pg_dump --format=custom` (this runbook) |
| Analytics Postgres (`ANALYSTOS_ANALYTICS_*_URL`) | staged snapshots of file/API sources, recipe and pipeline managed outputs | staged data can be re-staged from the sources; **managed outputs cannot** | the same `pg_dump`, on the analytics database |
| Artifact directory (`ANALYSTOS_ARTIFACT_DIR`) | recipe input/result snapshots, ML model artifacts, generated reports | a registered ML model or a report file no longer opens | a file backup (`--files` in the drill) |
| Upload directory (`ANALYSTOS_UPLOAD_DIR`) | uploaded CSV/Parquet/SQLite/DuckDB files | a file source must be uploaded again | a file backup |
| Redis, Temporal | counters, nudges, in-flight workflow state | in-flight runs are retried or fail visibly; nothing authoritative | not backed up |
| Neo4j (optional) | lineage projection | rebuilt from Postgres | not backed up |

Secrets are never in any of these (`secret_ref: env:NAME`); back up the secret manager separately.

## Back up

```bash
# control plane (repeat for the analytics database)
PGPASSWORD=... pg_dump --format=custom --no-owner --no-privileges \
  --host HOST --port 5432 --username USER --dbname analystos --file analystos-$(date -u +%Y%m%dT%H%M).dump
sha256sum analystos-*.dump > analystos.dump.sha256
tar czf artifacts-$(date -u +%Y%m%dT%H%M).tar.gz -C "$ANALYSTOS_ARTIFACT_DIR" .
```

Schedule it (cron, a Kubernetes CronJob) at the recovery point the pilot owner accepts; keep copies off the
database host. The recovery point is the time of the last dump: for a shorter one use WAL archiving / a managed
database's point-in-time recovery — not covered by this runbook or the drill.

## Restore

1. Stop the writers: API, workers, scheduler (`analystos scheduler`), so nothing writes during the restore.
2. Create an empty database and restore: `createdb analystos_restored && pg_restore --no-owner --no-privileges
   --exit-on-error --dbname analystos_restored analystos-<ts>.dump` (the dump carries `CREATE EXTENSION vector`;
   the restoring role needs the right to create it).
3. Check the schema: `SELECT version_num FROM alembic_version` must be the release's head
   (`alembic heads`); if the dump is older, run `analystos migrate` against it before starting the platform.
4. Restore the artifact and upload directories to the paths the settings name.
5. Point `ANALYSTOS_DATABASE_URL` at the restored database (or rename it into place) and start API, workers and
   scheduler. Runs that were in flight at the time of the dump resume or fail visibly (Temporal / the local
   orchestrator re-dispatch by idempotent task key); approvals keep their hash binding and expire on their own
   clock, so an approval that expired during the outage must be requested again.
6. Run the drill's checks against the restored database (below) before letting users in.

## The drill

`scripts/recovery_drill.py` rehearses steps 2–6 on a scratch database; it never writes to the source.

```bash
.venv/bin/python scripts/recovery_drill.py --source-url postgresql+psycopg://USER:PW@HOST:5432/analystos \
    --scratch analystos_drill --files "$ANALYSTOS_ARTIFACT_DIR" --report auto
```

It inventories the source (row count and content checksum of every table, Alembic revision), dumps it, restores
the dump into the scratch database, **destroys** that copy (`DROP DATABASE ... WITH (FORCE)`), restores again
(timed: the measured recovery time), and then checks on the recovered copy:

* every table has the same rows and checksum, and the Alembic revision is the same;
* runs: each run's workspace exists; no task or event points at a missing run;
* approvals: every payload still hashes to its `payload_hash` (so `verify_for_execution` behaves as before);
* evidence: every insight resolves its hypothesis and run; each verification record's `evidence_bundle_id`
  equals the hash of its insight's bundle wherever it did on the source;
* verification records: every fingerprint recomputes from its dependencies, and every dependency of a live
  record resolves (`evidence.verification.current_version`) to the same version as on the source;
* with `--files`: every restored file has its original sha256.

It exits 1 when any check fails and writes `docs/60-delivery/evidence/<date>-recovery-drill.md` (+ `.json`). Run
it after every schema change that touches the tables above, and before a pilot review.

## Limits

* A logical dump is a point-in-time copy; it is not continuous replication or failover (no HA is claimed).
* The drill measures recovery time for the drilled database's size on the drilled host only.
* A restore does not bring back Redis budget counters or in-flight Temporal state; nothing authoritative lives
  there.
