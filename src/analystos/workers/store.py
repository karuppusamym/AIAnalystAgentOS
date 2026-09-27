"""The artifact store isolated workers exchange data through (ADR-0022 decision 3, P7-06).

Blobs are content-addressed (sha256 of the bytes) and write-once; an artifact is (workspace, content
hash), so the same bytes in one workspace are one artifact whoever writes them. A task's outputs are
*bound* by (workspace, idempotency key, output name): the first write binds, an identical later write
(a retry that finished twice) gets the same artifact back, and a different one is refused, because a
retry must not silently change what a task produced. Files only: the store runs without the database,
so the conformance suite can stand it up anywhere.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from analystos.contracts.worker import ArtifactRef
from analystos.core.errors import BudgetExceeded, Conflict, InvalidInput, NotFound

MAX_KIND = 60


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_id_for(workspace_id: str, content_hash: str) -> str:
    return "wa_" + hashlib.sha256(f"{workspace_id}:{content_hash}".encode()).hexdigest()[:32]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}.{id(data)}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class ArtifactStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ paths
    def _blob(self, content_hash: str) -> Path:
        return self.root / "blobs" / content_hash[:2] / content_hash

    def _meta(self, artifact_id: str) -> Path:
        if not (artifact_id.startswith("wa_") and len(artifact_id) == 35 and artifact_id[3:].isalnum()):
            raise NotFound("no such artifact")
        return self.root / "meta" / f"{artifact_id}.json"

    def _binding(self, workspace_id: str, idempotency_key: str, name: str) -> Path:
        key = hashlib.sha256(f"{workspace_id}\x00{idempotency_key}\x00{name}".encode()).hexdigest()
        return self.root / "bindings" / key[:2] / f"{key}.json"

    # ------------------------------------------------------------------ artifacts
    def put(self, workspace_id: str, data: bytes, *, kind: str, media_type: str = "application/octet-stream",
            content_hash: str | None = None) -> ArtifactRef:
        """Store bytes (idempotent). `content_hash`, when the caller sent one, must match the bytes."""
        digest = sha256(data)
        if content_hash is not None and content_hash != digest:
            raise InvalidInput("the body does not match its declared sha256", details={"declared": content_hash,
                                                                                        "actual": digest})
        if not kind or len(kind) > MAX_KIND:
            raise InvalidInput("artifact kind is required (at most 60 characters)")
        blob = self._blob(digest)
        if not blob.exists():
            _atomic_write(blob, data)
        ref = ArtifactRef(artifact_id=artifact_id_for(workspace_id, digest), kind=kind, content_hash=digest,
                          media_type=media_type, bytes=len(data))
        meta = self._meta(ref.artifact_id)
        if not meta.exists():
            _atomic_write(meta, json.dumps({"workspace_id": workspace_id, "ref": ref.model_dump()}).encode())
        return self.ref(ref.artifact_id)[0]

    def ref(self, artifact_id: str) -> tuple[ArtifactRef, str]:
        """(ref, workspace id) of a stored artifact."""
        meta = self._meta(artifact_id)
        if not meta.exists():
            raise NotFound("no such artifact")
        body = json.loads(meta.read_bytes())
        return ArtifactRef.model_validate(body["ref"]), body["workspace_id"]

    def read(self, artifact_id: str) -> tuple[ArtifactRef, bytes]:
        ref, _ = self.ref(artifact_id)
        blob = self._blob(ref.content_hash)
        if not blob.exists():
            raise Conflict(f"artifact {artifact_id} has no content any more")
        data = blob.read_bytes()
        if sha256(data) != ref.content_hash:
            raise Conflict(f"artifact {artifact_id} does not match its content hash; it was modified and is refused")
        return ref, data

    # ------------------------------------------------------------------ task outputs
    def bound_outputs(self, workspace_id: str, idempotency_key: str, names: list[str] | set[str]) -> dict[str, ArtifactRef]:
        out = {}
        for name in names:
            path = self._binding(workspace_id, idempotency_key, name)
            if path.exists():
                out[name] = ArtifactRef.model_validate(json.loads(path.read_bytes())["ref"])
        return out

    def bind_output(self, workspace_id: str, idempotency_key: str, name: str, data: bytes, *, kind: str,
                    media_type: str, content_hash: str | None = None, max_bytes: int | None = None,
                    already_bound: int = 0) -> tuple[ArtifactRef, bool]:
        """(ref, created). The first write of an output binds it; the same content again returns that
        binding; different content under the same idempotency key is a Conflict."""
        existing = self.bound_outputs(workspace_id, idempotency_key, [name]).get(name)
        digest = sha256(data)
        if existing is not None:
            if existing.content_hash != digest:
                raise Conflict(f"output '{name}' of this task was already written with different content; a retry "
                               "must reproduce its first result", details={"output": name,
                                                                          "bound": existing.content_hash, "new": digest})
            return existing, False
        if max_bytes is not None and already_bound + len(data) > max_bytes:
            raise BudgetExceeded(f"the task's outputs would exceed its max_output_bytes ({max_bytes})",
                                 details={"limit": "output_bytes", "max_output_bytes": max_bytes,
                                          "bytes": already_bound + len(data)})
        ref = self.put(workspace_id, data, kind=kind, media_type=media_type, content_hash=content_hash)
        path = self._binding(workspace_id, idempotency_key, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"ref": ref.model_dump(), "name": name}).encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:  # a concurrent attempt bound it first: the same rule applies
            return self.bind_output(workspace_id, idempotency_key, name, data, kind=kind, media_type=media_type)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        return ref, True

    def stats(self) -> dict[str, Any]:
        """Counts for tests and the admin view: blobs, artifacts, bound outputs."""
        def count(sub: str) -> int:
            base = self.root / sub
            return sum(1 for p in base.rglob("*") if p.is_file() and not p.name.startswith(".")) if base.exists() else 0
        return {"blobs": count("blobs"), "artifacts": count("meta"), "bindings": count("bindings")}


def control_store(settings: Any = None) -> ArtifactStore:
    """The control plane's store: `<artifact_dir>/worker_artifacts`, served to workers by the API."""
    if settings is None:
        from analystos.core.config import get_settings

        settings = get_settings()
    return ArtifactStore(Path(settings.artifact_dir) / "worker_artifacts")
