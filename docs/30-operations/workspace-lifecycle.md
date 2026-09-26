# Workspace lifecycle and agent bindings

## Find an agent's tools and skills

Open **Operate → Capability registry**, choose a workspace, filter to **Agents**, and open an
agent. **Agent map** resolves the agent manifest's capability patterns against the loaded registry.
It shows each bound skill, tool or method, its determinism and certification, whether it is enabled
in that workspace, the direct tool IDs, model purposes, default actions and playbooks that use the
agent. The same data is returned under `bindings` by
`GET /api/capabilities?kind=Agent&workspace_id=<id>`.

This is the configured binding, not an execution trace. A run also applies the workspace policy,
role, data scope, tool gate, certification and approval checks. Inspect its bound plan and tool
execution records to see what actually ran.

## Disable and reactivate

An owner or platform admin can use **Workspaces → Disable workspace**. This changes `status` to
`disabled`; **Reactivate workspace** sets it back to `active`. The corresponding API is
`PATCH /api/workspaces/{id}` with `{"status":"disabled"}` or `{"status":"active"}`.
Normal workspace routes and new runs reject disabled workspaces. The scheduler rechecks owner
access before a firing, the query gateway checks the workspace even for a saved scope or cached
query, and approval execution checks it again before a side effect. The run engine stops claiming
new tasks and requests cancellation of an existing run. A query or external operation already in
progress at the instant of disable may finish. Reactivation retains the workspace's data and
settings.

## Deletion is currently archival

`DELETE /api/workspaces/{id}` sets `deleted_at` and disables the workspace. It removes it from
normal lists and access, but **does not erase data**. Do not present this route as a complete
deletion or rely on it for a retention or erasure requirement.

An owner can open **Workspaces → Data inventory** or call
`GET /api/workspaces/{id}/inventory` to inspect retained control rows, registered staged schemas,
build targets, local paths and publication identifiers. The endpoint remains available to owners
after disable or archive. Counts follow declared foreign keys from child tables to the workspace;
records without a declared ownership route and unverified external resources are outside its count.
It reads registry records and checks whether local paths exist; it does not inspect remote storage.

A complete purge is not implemented. It needs an inventory and verified cleanup across:

| Store | Workspace data to account for |
|---|---|
| Control Postgres | Workspace rows, runs, artifacts, knowledge, audit, approvals, query previews and related records |
| Analytics Postgres | Staged source schemas, built target relations, workspace reader/build roles |
| Local or mounted storage | Uploads, generated artifacts, build projects and logs |
| Other services | Redis cache and counters, Neo4j projection, Temporal histories, Superset objects created by publication |
| Backups and external sources | Backup retention and any data or BI objects outside this platform's ownership |

Dropping the control-plane workspace row alone can cascade some database rows but leaves the other
stores untouched. A future purge needs a dry-run inventory, explicit ownership checks for shared
target schemas and external objects, idempotent cleanup, and a completion record for each store.
