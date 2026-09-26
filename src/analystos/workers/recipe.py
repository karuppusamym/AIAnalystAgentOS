"""The first isolated consumer: a recipe's snapshot statement on `compute-py` (ADR-0023 + ADR-0022, P7-06).

Same job, same result as the in-process path (`recipes.execute.run_snapshot_job`), but the statement
runs in a credential-free worker: each input snapshot is published to the artifact store as its
exact file bytes, the worker verifies both the bytes' SHA-256 and the snapshot's content hash before
DuckDB sees them, and the result snapshot comes back as an output artifact that is verified again
before it enters the control plane's snapshot store.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from analystos.contracts.worker import RecipeSnapshotSpec, SnapshotInput, TaskEnvelope
from analystos.core.errors import Conflict
from analystos.core.ids import new_id, stable_hash
from analystos.recipes.execute import SnapshotStore
from analystos.workers.dispatch import capability_ref, default_budget, dispatch_isolated, raise_for_result
from analystos.workers.store import ArtifactStore, control_store

POOL = "compute-py"
PLATFORM_WORKSPACE = "_platform"


def recipe_envelope(job: dict[str, Any], store: SnapshotStore, artifacts: ArtifactStore) -> TaskEnvelope:
    workspace_id = job.get("workspace_id") or PLATFORM_WORKSPACE
    tables: dict[str, SnapshotInput] = {}
    refs = []
    for asset, digest in sorted(job["tables"].items()):
        store.get(digest)  # verified before it leaves the control plane
        ref = artifacts.put(workspace_id, store.path(digest).read_bytes(), kind="recipe_snapshot",
                            media_type="application/gzip")
        tables[asset] = SnapshotInput(artifact_id=ref.artifact_id, snapshot=digest)
        refs.append(ref)
    spec = RecipeSnapshotSpec(sql=job["sql"], tables=tables, max_rows=int(job["max_rows"]),
                              timeout_seconds=int(job["timeout_seconds"]))
    capability = capability_ref("recipe.snapshot", "recipe.snapshot")
    budget = default_budget(POOL)
    budget = budget.model_copy(update={"wall_seconds": max(budget.wall_seconds, int(job["timeout_seconds"]) + 30)})
    context_ref = f"recipe_run:{job['run_id']}" if job.get("run_id") else None
    return TaskEnvelope(task_id=new_id("wtask"), context_ref=context_ref, workspace_id=workspace_id,
                        capability=capability, spec=spec, input_artifacts=refs, budget=budget,
                        required_outputs=["result"],
                        idempotency_key=stable_hash({"capability": capability.model_dump(), "spec": spec.model_dump()})[:48])


def run_recipe_isolated(job: dict[str, Any], *, transport: Any = None, sink: Any = None, settings: Any = None,
                        secret: bytes | None = None, artifacts: ArtifactStore | None = None) -> dict[str, Any]:
    """`run_snapshot_job(job)` on the isolated pool; returns the same dict."""
    store = SnapshotStore(Path(job["artifact_dir"]) / "recipe_snapshots")
    artifacts = artifacts or control_store(settings)
    envelope = recipe_envelope(job, store, artifacts)
    result = raise_for_result(dispatch_isolated(POOL, envelope, transport=transport, sink=sink, settings=settings,
                                                secret=secret))
    _, data = artifacts.read(result.outputs["result"].artifact_id)
    body = json.loads(gzip.decompress(data))
    digest = result.result["result"]
    if stable_hash(body) != digest:
        raise Conflict("the worker's result snapshot does not match the id it reported; refused")
    if store.put(body["columns"], body["rows"]) != digest:
        raise Conflict("the worker's result snapshot changed when stored; refused")
    return {k: result.result[k] for k in ("result", "columns", "row_count", "truncated")}
