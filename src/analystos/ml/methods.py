"""The ML method pack (P5-04): `method.ml.<task>` capabilities and what binds a verdict to code.

Each manifest (`capabilities/builtin/ml/*.yaml`) points at one `MLMethod` here. ML methods are not part
of the analysis vocabulary (`methods/registry.py` skips `spec.family: ml`); they run only through the
experiment service and `run_ml_job`. The code digest covers every module of this package plus the
contract, so a verdict's method dependency changes whenever the code that produced it does.
"""
from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from analystos.core.ids import stable_hash

PACKAGE_DIR = Path(__file__).resolve().parent
CONTRACT = PACKAGE_DIR.parent / "contracts" / "work.py"
LIBRARIES = ("scikit-learn", "statsmodels", "numpy", "scipy", "pandas")


@dataclass(frozen=True)
class MLMethod:
    name: str  # ml.<task>
    task: str
    evidence: tuple[str, ...]

    def learner(self) -> Any:
        from analystos.ml.anomaly import Anomaly
        from analystos.ml.cluster import Cluster
        from analystos.ml.forecast import Forecast
        from analystos.ml.tabular import Tabular

        return {"classify": Tabular, "regress": Tabular, "forecast": Forecast, "cluster": Cluster, "anomaly": Anomaly}[self.task]


classify = MLMethod("ml.classify", "classify", ("threshold", "calibration", "confusion", "guardrails"))
regress = MLMethod("ml.regress", "regress", ("residuals", "guardrails"))
forecast = MLMethod("ml.forecast", "forecast", ("abs_error_by_horizon", "coverage_80", "coverage_95"))
cluster = MLMethod("ml.cluster", "cluster", ("stability",))
anomaly = MLMethod("ml.anomaly", "anomaly", ("threshold", "alarm_rate"))
BY_TASK = {m.task: m for m in (classify, regress, forecast, cluster, anomaly)}


def capability_id(task: str) -> str:
    return f"method.ml.{task}"


_digest: tuple[tuple[tuple[str, int, int], ...], str] | None = None


def code_digest() -> str:
    """sha256 over the ML package's modules and the MLSpec contract (cached per file mtime and size)."""
    global _digest
    files = sorted([*PACKAGE_DIR.glob("*.py"), CONTRACT])
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _digest is None or _digest[0] != key:
        h = hashlib.sha256()
        for p in files:
            h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
        _digest = (key, h.hexdigest())
    return _digest[1]


def environment() -> dict[str, Any]:
    libs = {}
    for lib in LIBRARIES:
        try:
            libs[lib] = metadata.version(lib)
        except metadata.PackageNotFoundError:
            libs[lib] = None
    return {"python": platform.python_version(), "libraries": libs}


def environment_digest() -> str:
    return stable_hash(environment())


def method_version(ref: str) -> str | None:
    """The `method` verification dependency of an ML verdict: manifest id + version + code digest."""
    from analystos.capabilities import registry

    task = ref.split(".", 1)[1] if ref.startswith("ml.") else None
    if task not in BY_TASK:
        return None
    m = registry.current().manifests.get(capability_id(task))
    if m is None:
        return None
    return stable_hash({"id": m.id, "version": m.version, "code": code_digest()})
