"""ML jobs on the isolated `compute-ml` pool (ADR-0024 + ADR-0022, P5-01/P7-06).

Same job, same result as the in-process path (`ml.jobs.run_ml_job`), but it runs in a credential-free
worker, and the only place a model package is unpickled is that worker. The control plane publishes
the job's input files (the dataset snapshot and, for scoring, the package) to the artifact store as
their exact bytes; the worker lays them out as a private artifact directory, runs `run_ml_job` there,
and returns two outputs: `result` (the job's dict, minus wall-clock timings, which come back inline so
the artifact stays reproducible) and `files` (a tar of every file the job wrote: split memberships, the
package, scored snapshots). The control plane re-verifies each file against its content address before
it enters `<artifact_dir>`, so a worker cannot plant a file under a name its bytes do not hash to.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path
from typing import Any

from analystos.contracts.worker import MLJobSpec, TaskEnvelope
from analystos.core.errors import Conflict, InvalidInput
from analystos.core.ids import new_id, stable_hash
from analystos.workers.dispatch import capability_ref, default_budget, dispatch_isolated, raise_for_result
from analystos.workers.store import ArtifactStore, control_store

POOL = "compute-ml"
PLATFORM_WORKSPACE = "_platform"
OUTPUTS = ["result", "files"]
_HEX = r"[0-9a-f]{64}"
# Every file an ML job reads or writes, by kind: how its name is its content address.
LAYOUT = (
    (re.compile(rf"^ml_snapshots/[0-9a-f]{{2}}/({_HEX})\.json\.gz$"), "json"),
    (re.compile(rf"^ml/memberships/[0-9a-f]{{2}}/({_HEX})\.json\.gz$"), "json"),
    (re.compile(rf"^ml/packages/[0-9a-f]{{2}}/({_HEX})\.pkl$"), "bytes"),
)


def check_file(rel: str, data: bytes) -> None:
    """Refuse a file whose path is not an ML store path or whose bytes do not hash to its name."""
    for pattern, how in LAYOUT:
        m = pattern.match(rel)
        if m is None:
            continue
        digest = m.group(1)
        if rel.split("/")[-2] != digest[:2]:
            break
        if how == "bytes":
            actual = hashlib.sha256(data).hexdigest()
        else:
            try:
                actual = stable_hash(json.loads(gzip.decompress(data)))
            except (OSError, ValueError, EOFError):
                actual = ""
        if actual != digest:
            raise Conflict(f"ML file {rel} does not match its content address; refused", details={"file": rel})
        return
    raise InvalidInput(f"{rel!r} is not an ML store file", details={"file": rel})


def _volatile(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(stable result, wall-clock timings): timings differ on every run and never belong in an artifact."""
    stable = dict(result)
    timings: dict[str, Any] = {}
    if "seconds" in stable:
        timings["seconds"] = stable.pop("seconds")
    if isinstance(stable.get("trials"), list):
        timings["trials"] = [t.get("seconds") if isinstance(t, dict) else None for t in stable["trials"]]
        stable["trials"] = [{k: v for k, v in t.items() if k != "seconds"} if isinstance(t, dict) else t
                            for t in stable["trials"]]
    return stable, timings


def _restore(stable: dict[str, Any], timings: dict[str, Any]) -> dict[str, Any]:
    out = dict(stable)
    if isinstance(out.get("trials"), list) and "trials" in timings:
        out["trials"] = [{**t, "seconds": s} if isinstance(t, dict) else t
                         for t, s in zip(out["trials"], timings["trials"], strict=True)]
    if "seconds" in timings:
        out["seconds"] = timings["seconds"]
    return out


def tar_files(root: Path, skip: set[str]) -> tuple[bytes, list[str]]:
    """A deterministic tar of every file under `root` except `skip` (sorted, zero mtimes and owners)."""
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                   if p.is_file() and ".tmp" not in p.name and p.relative_to(root).as_posix() not in skip)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in names:
            data = (root / name).read_bytes()
            check_file(name, data)
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue(), names


def untar_files(data: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                raise InvalidInput(f"the ML job's file bundle holds a non-file entry {member.name!r}; refused")
            f = tar.extractfile(member)
            body = f.read() if f is not None else b""
            check_file(member.name, body)
            files[member.name] = body
    return files


# ------------------------------------------------------------------------------------ worker side
def ml_job(spec: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The `ml.job` handler: `run_ml_job` over the task's verified input files in a private artifact dir."""
    from analystos.ml.jobs import run_ml_job

    job = dict(spec.get("job") or {})
    files: dict[str, str] = job.pop("files", {}) or {}
    root = ctx.scratch / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    for rel, artifact_id in sorted(files.items()):
        data = ctx.read_input(artifact_id)  # the supervisor verified the bytes against the envelope's ref
        check_file(rel, data)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    ctx.progress("inputs verified", 0.05, files=len(files), kind=job.get("kind", "train"))
    result = run_ml_job({**job, "artifact_dir": str(root)})
    stable, timings = _volatile(result)
    body = json.dumps(stable, sort_keys=True, default=str).encode()
    bundle, names = tar_files(root, set(files))
    ctx.write_output("result", body, kind="ml_job_result", media_type="application/json")
    ctx.write_output("files", bundle, kind="ml_job_files", media_type="application/x-tar")
    return {"status": stable.get("status"), "result_sha256": hashlib.sha256(body).hexdigest(), "files": names,
            "timings": timings}


# ------------------------------------------------------------------------------------ control-plane side
def input_files(job: dict[str, Any]) -> dict[str, bytes]:
    """The files `run_ml_job` would read for this job, each verified in the control plane's store first.
    A file the store no longer has is left out: the worker then fails exactly as the inline job would."""
    from analystos.ml.store import MLStore, snapshots

    root = Path(job["artifact_dir"])
    out: dict[str, bytes] = {}
    if job.get("snapshot"):
        store = snapshots(root)
        path = store.path(job["snapshot"])
        if path.exists():
            store.get(job["snapshot"])
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    if job.get("package_hash"):
        ml = MLStore(root)
        path = ml.package_path(job["package_hash"])
        if path.exists():
            out[path.relative_to(root).as_posix()] = ml.package_bytes(job["package_hash"])
    return out


def ml_envelope(job: dict[str, Any], artifacts: ArtifactStore) -> TaskEnvelope:
    from analystos.ml.jobs import caps_of

    workspace_id = job.get("workspace_id") or PLATFORM_WORKSPACE
    files, refs = {}, []
    for rel, data in sorted(input_files(job).items()):
        ref = artifacts.put(workspace_id, data, kind="ml_input",
                            media_type="application/octet-stream" if rel.endswith(".pkl") else "application/gzip")
        files[rel] = ref.artifact_id
        refs.append(ref)
    worker_job = {**{k: v for k, v in job.items() if k != "artifact_dir"}, "files": files}
    spec = MLJobSpec(job=worker_job)
    capability = capability_ref("ml.job", "ml.job")
    caps = caps_of(job)
    budget = default_budget(POOL)
    budget = budget.model_copy(update={"wall_seconds": max(budget.wall_seconds, caps["max_seconds"] + 300),
                                       "cpu_seconds": max(budget.cpu_seconds, caps["max_seconds"] + 300),
                                       "max_trials": caps["max_trials"]})
    # One key per call: a retry of a lost worker reuses it; a new call (a re-run) binds its own outputs.
    key = stable_hash({"capability": capability.model_dump(), "spec": spec.model_dump(), "call": new_id("mlcall")})[:48]
    return TaskEnvelope(task_id=new_id("wtask"), workspace_id=workspace_id, capability=capability, spec=spec,
                        input_artifacts=refs, budget=budget, required_outputs=list(OUTPUTS),
                        context_ref=f"ml:{job.get('kind', 'train')}", idempotency_key=key)


def materialize(files: dict[str, bytes], artifact_dir: Path) -> None:
    """Write the job's verified files into the control plane's ML store (write-once; an existing file must match)."""
    for rel, data in sorted(files.items()):
        check_file(rel, data)
        path = artifact_dir / rel
        if path.exists():  # same content address: the same content (a gzip may differ only in its framing)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)


def run_ml_isolated(job: dict[str, Any], *, transport: Any = None, sink: Any = None, settings: Any = None,
                    secret: bytes | None = None, artifacts: ArtifactStore | None = None) -> dict[str, Any]:
    """`run_ml_job(job)` on the isolated `compute-ml` pool; returns the same dict and leaves the same files."""
    artifacts = artifacts or control_store(settings)
    envelope = ml_envelope(job, artifacts)
    result = raise_for_result(dispatch_isolated(POOL, envelope, transport=transport, sink=sink, settings=settings,
                                                secret=secret))
    _, body = artifacts.read(result.outputs["result"].artifact_id)
    if hashlib.sha256(body).hexdigest() != result.result.get("result_sha256"):
        raise Conflict("the worker's ML result does not match the hash it reported; refused")
    _, bundle = artifacts.read(result.outputs["files"].artifact_id)
    files = untar_files(bundle)
    if sorted(files) != sorted(result.result.get("files") or []):
        raise Conflict("the worker's ML file bundle does not match the files it reported; refused")
    materialize(files, Path(job["artifact_dir"]))
    return _restore(json.loads(body), result.result.get("timings") or {})
