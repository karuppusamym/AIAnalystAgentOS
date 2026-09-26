"""Isolated, resource-limited execution of allow-listed numeric Python (spec §47, P4-02).

`sandbox.isolation` picks the backend (container or namespaces) and refuses without one; see
`analystos.sandbox.runner` for the layers and what they do and do not protect against.
"""
from analystos.sandbox.runner import SandboxResult, check_code, run_python

__all__ = ["SandboxResult", "check_code", "run_python"]
