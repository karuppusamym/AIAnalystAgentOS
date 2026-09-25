# ADR-0009 — Scheduling and continuous analytics

**Status:** accepted (2026-09-25, increment 2)

**Context.** v1 §37–§38 require recurring re-analysis, report delivery and monitoring. Risks:
double firing (several scheduler replicas, restarts), stale permissions (a schedule created by
someone who has since lost access), silent external side effects, and monitors that alert on noise.

**Decisions.**
1. **Claim-then-execute scheduler** (`analystos scheduler`): due schedules are selected with
   `FOR UPDATE SKIP LOCKED`, `next_run_at` is advanced and a `schedule_run` row is inserted under
   a unique `fire_key` in the same transaction. Replicas and retries cannot double-fire; every
   firing is audited (SCH-005). Minimum interval 15 minutes. Cron is evaluated in the schedule's
   IANA timezone.
2. **Permissions at fire time.** A schedule executes as its owner with the owner's *current* role;
   a revoked owner makes the firing fail visibly with a notification.
3. **Scheduled runs do not publish by default** (`publish: skip`). `propose` creates the normal
   hash-bound approval; nothing is published unattended in this release.
4. **What changed** is computed in `finalize` by matching findings on their claim
   (method, outcome, segment, top group) rather than wording; KPI deltas by metric name.
5. **Monitors** reuse the analytical dataset and validated KPI expressions through the gateway
   (same scope, audit and cache). Detection is deterministic: threshold, robust z-score against a
   trailing median/MAD baseline, change point (skills), and data-quality regression versus a
   recorded baseline. Alerts are de-duplicated per monitor and period.
6. **JEV triage is escalate-only** (`alert_triage`): P(material) ≥ 0.8 raises severity to
   critical; it can never suppress an alert.
7. **Automatic investigation** (MON-005) starts an analysis run only when the monitor opts in,
   the workspace autonomy is ≥ 3 and policy allows `run_analysis` for the monitor owner; the run
   is labelled `origin: alert` and does not publish.
8. **Notifications are in-app only.** Email/chat/webhook delivery is an external side effect and
   will go through the approval model when added.

**Consequences.** The scheduler is a separate process role (compose service `scheduler`).
Reports are rendered deterministically from persisted evidence (`ReportData`) and stored with a
content hash that is re-checked on download.
