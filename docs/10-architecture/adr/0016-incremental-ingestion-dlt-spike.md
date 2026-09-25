# ADR-0016 — Incremental ingestion from non-SQL sources: dlt versus the staged loader (P4-E05 spike)

**Status:** proposed (2026-09-25, spike P4-E05; follows ADR-0014 decision 5)

**Context.** Non-SQL sources (ServiceNow, REST, files) are extracted by their connector and loaded
by `staging/loader.py` into `analytics.src_<source>` with `COPY` into a load table and an atomic
swap. Every refresh is a full reload. ADR-0014 asked whether **dlt** should do incremental loads
instead, into the analytics database or the customer's warehouse.

**Measurement.** `scripts/spike_dlt_vs_staged.py` loads the ServiceNow mock's `incident` table
(20,000 rows) into a scratch Postgres 16 database on the same host, both ways, page size 1,000.
dlt 1.30.0 (`dlt[postgres]`, own venv) uses `dlt.sources.incremental("sys_updated_on")` with merge
on `sys_id`; the staged path is today's `ServiceNowConnector.extract` → `StagingLoader.load`.
Two samples per mode on a shared, noisy machine (other agents were running test suites); ranges shown.

| Run | Staged loader (display values, as configured today) | Staged loader (raw values) | dlt incremental (raw values) |
|---|---|---|---|
| Initial full load | 2.8–3.4 s · 21 requests · 28.0 MB · 20,000 rows | 3.6 s · 21 req · 11.6 MB | 6.5–7.8 s · 21 req · 11.6 MB · 20,000 rows |
| Refresh, nothing changed | 2.1–3.9 s · 21 req · 28.0 MB · 20,000 rows reloaded | 2.8 s · 11.6 MB | **0.27–0.31 s · 1 req · 561 B · 0 rows** |
| Refresh, 5 % changed | 2.5–4.7 s · full reload | 1.9 s · full reload | **1.0–1.2 s · 2 req · 0.57 MB · 1,001 rows** (window replayed cold, see caveats) |

**Reading.**
- Incremental is the win, not dlt itself: a watermark query moves ~0.005 % of the bytes when nothing
  changed and ~5 % when 5 % changed, and finishes 10× faster. A full load through dlt is 2–3× slower
  than the loader's `COPY` (dlt normalizes to files, loads a `_staging` dataset, then merges).
- dlt brings its own objects and behaviour into the analytics database: `_dlt_loads`,
  `_dlt_pipeline_state`, `_dlt_version`, a `<dataset>_staging` schema, schema inference and evolution,
  and a local pipeline working directory. None of that knows about per-workspace reader roles, the
  atomic swap, the snapshot population record that reaches verification (P4-C12), or the crawler's
  fingerprints and drift (ADR-0010); each would have to be re-attached around it.
- Pagination under an incremental cursor has a trap the spike hit: the cursor must be fixed for the
  whole pagination (dlt advances `last_value` while rows are yielded; offset paging against a moving
  filter silently skipped 75 % of the rows in the first attempt). Any implementation needs a test for it.
- Incremental by `sys_updated_on` does not see deletes. ServiceNow needs `sys_audit_delete` (or a
  periodic full reconcile); the full reload handles deletes today by construction.

**Decision.**
1. **Do not add dlt to the platform runtime.** Add watermark incremental to the staged loader
   instead, for source tables that declare a reliable watermark column and key (ServiceNow:
   `sys_updated_on` + `sys_id`): fetch `>= last watermark` with a cursor fixed per pagination, `COPY`
   into the load table, `MERGE` on the key into the staged table inside the same transaction, keep
   the per-workspace grants, and record the watermark and the merged row count in the snapshot record.
   A scheduled full reconcile (default weekly) catches deletes. This is the same rule as TRN-003 for
   dbt: incremental only with a reliable watermark, otherwise full refresh.
2. **dlt stays an option on the customer's side**, like dbt in ADR-0014: when a customer wants
   non-SQL data landed in *their* warehouse, a generated dlt pipeline can run on their runner under a
   BuildGateway-style approval and write identity. That is a later increment and needs its own row.
3. Follow-up row for the tracker (not opened here): "staged loader watermark incremental + delete
   reconcile" with tests for the fixed-cursor pagination, merge idempotency and delete handling.

**Caveats of the measurement.** One table, one host, two samples, noisy machine; the mock's data is
static, so the "5 % changed" run replays the newest 5 % window into a fresh dataset (it measures the
fetch + normalize + load of that window, not a merge onto the existing 20,000 rows); the staged
display-value run moves 2.4× the bytes because it also fetches display values, which the raw runs
do not. The numbers decide the direction (incremental, in the loader), not a performance budget.

**Consequences.** No new runtime dependency or schema objects in the analytics database; the
incremental work is ours to test and maintain. The dlt option for customer warehouses stays open.
