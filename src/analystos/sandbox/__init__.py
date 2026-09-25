"""Resource-limited subprocess execution of allow-listed numeric Python (spec §47).

Defense in depth only; see `analystos.sandbox.runner` for what it does and does not protect against.
"""
from analystos.sandbox.runner import SandboxResult, check_code, run_python

__all__ = ["SandboxResult", "check_code", "run_python"]
