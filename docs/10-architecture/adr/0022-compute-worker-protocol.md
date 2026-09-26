# ADR-0022 — One compute-worker protocol for heavy data-science, ML and transformation work

**Status:** proposed (2026-09-26, spec v4 §4; tracker P7-06). Implements the "compute worker"
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
