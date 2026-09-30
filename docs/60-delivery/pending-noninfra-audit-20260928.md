# Non-infrastructure unfinished work audit — 2026-09-28

The delivery tracker remains the status authority. This audit separates the unfinished rows that
need product code from rows whose acceptance needs a person, a funded model run, or a live external
system. Infrastructure deployment, identity, image sizing and connector certification are outside
this audit's scope.

| Tracker row | Remaining acceptance | Dependency |
|---|---|---|
| N-2 | Analysis-driven changes to an imported dashboard's charts, metrics, filters and layout | Scoped import and approved in-place title change are built; N-2 remains Partial. Live Superset inspection and import passed; the approved title update has only automated-test coverage. |
| P4-08 | Paired practitioner baseline and live-model held-out run | Human participants and a funded model provider. |
| P7-14 | Owner sign-off on donor parity and freeze | Named owner decision. |
| P8-03 / P4-T04 | Provider-reported prompt-cache share | Cache-capable live model and credits. |
| P4-V01 / P4-V02 | Live-model benchmark reports and pilot threshold | Funded model run; P4-V02 threshold needs an owner. |
| N-12 | Classification error rates across domains and languages | Labelled live-source sample and model run. |
| P4-K09 | Live Atlas certification | Live Atlas instance. |
| P4-X05 | Real Superset/dbt MCP server registration | Live MCP server. Platform exact-host allowlist is now enforced. |

Closed in this follow-up: P8-16 joined analysis, N-3 approved report/alert email and webhook
delivery (approval required on every schedule fire), and N-6-font bundled Unicode PDF support.
The tracker and capability register hold their tests and acceptance details.

Several tracker rows marked Done also carry explicit live-browser or real-source follow-ups. Those
remain evidence gaps, and their Done status should be reconsidered against the stated acceptance
criteria when they are next reconciled.

Validation of this follow-up: 144 focused unit tests and 4 Postgres integration tests passed. A
full `pytest -m "not integration"` run stopped at the existing Windows worker transport issue:
`SubprocessTransport._readline` calls `select.select` on a pipe and raises WinError 10038. That
worker transport is infrastructure related and was left untouched.

The rebuilt Compose API, web, scheduler and workers were recreated on 2026-09-28. API health
reported Postgres, Redis, Superset and model services up; sandboxed Python code steps remained
unavailable (infrastructure). The live retail journey passed 69/69 checks in workspace
`ws_272b2ca5401b`: measured joins, joined findings (West late deliveries and Electronics
returns), evidence, PDF report/download, ML baseline comparison and monitor evaluation.
Live Superset dashboard 16 inspection and import passed for `ws_9a4b56958e2e`.
