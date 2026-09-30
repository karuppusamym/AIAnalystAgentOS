"""Wheel build hook: bundle the repository files AnalystOS reads at run time under analystos/_data.

A source checkout reads config/, migrations/, packs/ from the repository (core/config.py DATA_ROOT); an installed
wheel has no repository, so the same files travel inside the package. Only live connector evidence is taken from
docs/: the API derives `certified` from it (connectors/certification.py).
"""
from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

DATA_PREFIX = "analystos/_data"
DATA_DIRS = ("config", "migrations", "packs")
DATA_FILES = ("alembic.ini",)
EVIDENCE_DIR = "docs/60-delivery/evidence"
EVIDENCE_GLOB = "connector-*.md"
SKIP_PARTS = {"__pycache__"}


def bundled_files(root: Path) -> dict[str, str]:
    """{source path: path inside the wheel} for every data file the wheel carries."""
    out: dict[str, str] = {}
    for name in DATA_FILES:
        out[str(root / name)] = f"{DATA_PREFIX}/{name}"
    for name in DATA_DIRS:
        for path in sorted((root / name).rglob("*")):
            if path.is_file() and not SKIP_PARTS & set(path.parts) and path.suffix not in {".pyc", ".pyo"}:
                out[str(path)] = f"{DATA_PREFIX}/{path.relative_to(root).as_posix()}"
    for path in sorted((root / EVIDENCE_DIR).glob(EVIDENCE_GLOB)):
        out[str(path)] = f"{DATA_PREFIX}/{path.relative_to(root).as_posix()}"
    return out


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":  # the checkout itself is the data root
            return
        build_data["force_include"].update(bundled_files(Path(self.root)))
