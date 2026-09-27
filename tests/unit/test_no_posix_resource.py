"""Without POSIX `resource` (Windows) the modules still import, and nothing runs without limits."""
from __future__ import annotations

import subprocess
import sys

import pytest

from analystos.core.errors import UnsupportedCapability
from analystos.sandbox import runner
from analystos.workers import isolation


def test_modules_import_when_resource_is_missing():
    code = ("import sys; sys.modules['resource'] = None\n"
            "import analystos.workers.isolation as i, analystos.sandbox.runner as r\n"
            "assert i.resource is None and r.resource is None")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_isolated_job_refuses_without_rlimits(monkeypatch):
    monkeypatch.setattr(isolation, "resource", None)
    with pytest.raises(isolation.WorkerMisconfigured, match="POSIX resource limits"):
        isolation.job_rlimits({"cpu_seconds": 5, "memory_mb": 256, "max_output_bytes": 1024})


def test_host_sandbox_refuses_without_rlimits(monkeypatch):
    monkeypatch.setattr(runner, "resource", None)
    with pytest.raises(UnsupportedCapability, match="POSIX resource limits"):
        runner._host_limits(256, 5, 64, 8)
