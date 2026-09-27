"""Content-addressed ML files under `<artifact_dir>/ml` (P5-05): packages and split memberships.

A package is written only by `run_ml_job` and read only through `load_package`, which refuses a file
whose bytes no longer hash to its name. The service layer adds the other half of the rule: a hash is
loadable only when a platform-produced experiment record names it (`services/ml.load_package`), so a
file dropped into the store, or uploaded, never runs.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import pickle
from pathlib import Path
from typing import Any

from analystos.core.errors import Conflict, InvalidInput, NotFound
from analystos.core.ids import stable_hash

PACKAGE_FORMAT = "analystos.ml.package.v1"


def _check(digest: str) -> str:
    if not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        raise InvalidInput("not a content hash")
    return digest


class MLStore:
    def __init__(self, artifact_dir: Path | str) -> None:
        self.root = Path(artifact_dir) / "ml"

    def package_path(self, digest: str) -> Path:
        return self.root / "packages" / _check(digest)[:2] / f"{digest}.pkl"

    def _write(self, path: Path, data: bytes) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def put_package(self, obj: dict[str, Any]) -> tuple[str, int]:
        data = pickle.dumps({"format": PACKAGE_FORMAT, **obj}, protocol=5)
        digest = hashlib.sha256(data).hexdigest()
        self._write(self.package_path(digest), data)
        return digest, len(data)

    def package_bytes(self, digest: str) -> bytes:
        path = self.package_path(digest)
        if not path.exists():
            raise NotFound(f"package {digest[:12]} is not in the store")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise Conflict(f"package {digest[:12]} does not match its content hash; it was modified and is refused")
        return data

    def file_hash(self, digest: str) -> str | None:
        """The current sha256 of a stored package file (None when missing): the verification resolver."""
        path = self.package_path(digest)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    def load_package(self, digest: str) -> dict[str, Any]:
        """Unpickle only bytes that hash to `digest` (callers first check the platform record)."""
        obj = pickle.loads(self.package_bytes(digest))  # noqa: S301 - integrity-checked, platform-produced only
        if not isinstance(obj, dict) or obj.get("format") != PACKAGE_FORMAT:
            raise Conflict(f"package {digest[:12]} is not a platform package")
        return obj

    def put_json(self, kind: str, obj: Any) -> str:
        digest = stable_hash(obj)
        self._write(self.root / kind / digest[:2] / f"{digest}.json.gz",
                    gzip.compress(json.dumps(obj, sort_keys=True, default=str).encode(), mtime=0))
        return digest

    def get_json(self, kind: str, digest: str) -> Any:
        path = self.root / kind / _check(digest)[:2] / f"{digest}.json.gz"
        if not path.exists():
            raise NotFound(f"{kind} {digest[:12]} is not in the store")
        obj = json.loads(gzip.decompress(path.read_bytes()))
        if stable_hash(obj) != digest:
            raise Conflict(f"{kind} {digest[:12]} does not match its content hash")
        return obj


def snapshots(artifact_dir: Path | str) -> Any:
    from analystos.recipes.execute import SnapshotStore

    return SnapshotStore(Path(artifact_dir) / "ml_snapshots")
