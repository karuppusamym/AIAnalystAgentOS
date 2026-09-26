# ADR-0022 — One compute-worker protocol for heavy data-science, ML and transformation work

**Status:** accepted, implemented by P7-06 (2026-09-26; proposed the same day, spec v4 §4). Implements the "compute worker"
boundary of [ADR-0011 (workspace)](0011-workspace-workflows-and-evidence.md) and the container
sandbox of P4-02. Source: the
[2026-09-26 comparison review](../../70-reviews/2026-09-26-agent-os-comparison-review.md) §9.

**Context.** Per-workload Temporal queues already exist (`config/task_queues.yaml`,
`workflows/queues.py`, P4-S01): `analysis`, `compute` (process executor, so a CPU-bound statistic
can't starve heartbeats) and `publish`, each its own worker pool. They separate *load*, not *trust*:
a `compute` worker runs the same code with the same database and provider credentials. The work
this increment adds needs trust separation too: model training with a search budget, backtests,
recipe execution over a multi-GB snapshot, Spark submission. Running it in-process shares memory,
CPU and credentials with the control plane. The sandbox (`sandbox/runner.py`) is explicitly defence
in depth, not a boundary, and handles one allow-listed script, not a job. The comparison review
proposed separate specialist repositories as workers; ADR-0018 keeps one codebase instead, so the
boundary is a **process/container protocol inside this repository**, not a repository split.

**Decision.**

1. **Two execution classes for a capability**, declared in its manifest: `inline` (default; runs in
   the Temporal activity on its workload queue, as today) and `worker: <pool>` (dispatched to an
   *isolated* pool: `compute-py`, `compute-ml`, later `spark-submit`). Isolated pools are new
   entries in `config/task_queues.yaml` with `isolated: true`. They run as a separate deployment
   with their own resource limits, no database or provider credentials, and egress denied except to
   the artifact store. `analystos worker --queues compute-ml` starts one.
2. **`TaskEnvelope`** (contract in `contracts/worker.py`, exported to `contracts/*.json`):
   `task_id, run_id, workspace_id, work_order_id, capability {id, version, content_hash},
   spec (the typed AnalysisSpec | PipelineSpec | MLSpec | ExperimentSpec), input_artifacts[ArtifactRef],
   context_ref, policy_ref, budget {cpu_seconds, memory_mb, wall_seconds, max_output_bytes, max_trials},
   required_outputs[], trace_parent, deadline, idempotency_key`. Workers return `ArtifactRef`s and a
   structured result/error, never large inline payloads.
3. **`ArtifactRef`** = `{artifact_id, version, kind, content_hash, media_type, bytes}`. Workers read
   inputs and write outputs through the artifact store with a **scoped capability token**: signed,
   short-lived, bound to `(task_id, artifact_ids, verbs)`; the store rejects any other object. A
   worker never receives a source or provider secret; a worker that needs data gets an immutable
   snapshot artifact produced by the gateway, never a connection (CLAUDE.md rule 4).
4. **Model calls from a worker** (rare; e.g. a feature-name explanation) go back through the
   control plane's router over a narrow callback bound to the same token and purpose budget;
   workers have no provider keys.
5. **Events.** Workers stream `task.started | step.completed | artifact.created | task.progress |
   task.completed | task.failed` over Temporal heartbeats/signals; the control plane persists them
   (existing event bus). A lost worker is a Temporal activity timeout and a retry under the same
   idempotency key; outputs are content-addressed, so a retry that finishes twice writes one artifact.
6. **Conformance suite** (`tests/conformance/worker/`): envelope round-trip, token scope refusal,
   egress refusal, budget kill, idempotent retry, structured error mapping to `core/errors.py`.
   A new pool (or an out-of-tree worker implementation) must pass it before it's enabled.
7. **External agents** (A2A, MCP clients) stay on the MCP surface (ADR-0011 capability platform,
   spec v3 §3.7); they are callers, not workers, and never receive a `TaskEnvelope` with a token.

**Consequences.** Heavy work can't exhaust the control plane or reach credentials; a pool can
scale independently (Kubernetes HPA on queue depth). The protocol is the seam a separate
implementation (including a future out-of-repo specialist) would plug into, without making it an
orchestrator. Cost: artifact round-trips for inputs (a snapshot export per task) and a second
deployment to operate; `inline` remains the default so small skills pay nothing.

**Implementation (P7-06, 2026-09-26).** Where each decision lives, and what differs from the text above:

* Contracts: `contracts/worker.py` (`TaskEnvelope`, `ArtifactRef`, `TaskDispatch`, `TaskResult`, typed specs
  `RecipeSnapshotSpec | MLJobSpec | ProbeSpec`), exported to `contracts/task_*.schema.json` and `artifact_ref`.
  `capability.content_hash` is the SHA-256 of the handler module's source: a worker running other code refuses.
* Pools: `compute-py`, `compute-ml` in `config/task_queues.yaml` (`isolated: true`, default budgets), never part
  of `--queues all`; opt-in per installation with `ANALYSTOS_ISOLATED_POOLS`. A manifest declares the need as
  `requires: [pool:compute-ml]` (reported unavailable with the remedy); `workers/dispatch.require_pool` refuses
  a direct call the same way. The manifest `worker: <pool>` execution class is not modelled separately: the
  pool follows from the spec kind (`workers/handlers.py`).
* Tokens: `workers/tokens.py` (HMAC-SHA256, domain-separated from session JWTs; task, workspace, idempotency
  key, readable artifact ids, writable output names, verbs `read|write|model`, purposes, output-byte cap).
* Artifact store: `workers/store.py` (content-addressed blobs; outputs bound per workspace + idempotency key +
  name: identical rewrite = same artifact, different content = conflict), served by the token-only routes in
  `api/routers/worker.py`. It holds worker artifacts only; registry artifacts are not exposed to workers.
* Worker: `analystos worker --queues compute-py` -> `workers/main.py` (environment refusal and scrub, egress
  guard, then a transport), `workers/runtime.py` (supervisor: hash check, verified downloads, job child with
  rlimits and an empty network namespace where available, uploads), `workers/child.py` (structured errors).
* Transports: Temporal (`IsolatedTaskWorkflow` on the analysis queue; `run_isolated_task` on `<prefix>-<pool>`;
  events as signals, persisted by `record_task_events`) and a local subprocess pool for the lite profile.
  Decision 5's `step.completed` and `artifact.created` event types are folded into `task.progress` and
  `task.completed` (which lists the output artifact ids).
* Model callback (decision 4): `POST /api/worker/model`, the token's purposes and call cap, the control
  plane's router with the workspace policy. A job child has no network, so only the supervisor can call it
  today; exposing it to a job goes through the supervisor.
* First consumer: recipe snapshot jobs (`workers/recipe.py`) when `compute-py` is configured.
* Conformance: `tests/conformance/worker/` (any implementation via `ANALYSTOS_CONFORMANCE_IMPL`); green for
  both pools on the local subprocess transport and on Temporal (dev server). Not verified here: the compose
  `isolated` network and the Helm NetworkPolicy under a real CNI (no Docker or cluster in the test environment).
